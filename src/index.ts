import { Plugin } from "@opencode/plugin"
import { spawn, type ChildProcess } from "node:child_process"
import path from "node:path"
import { createInterface } from "node:readline"
import { fileURLToPath } from "node:url"

/** Resolve the driver relative to this module, never process.cwd(). */
const DRIVER_PATH = path.resolve(fileURLToPath(new URL("../driver/kernel_driver.py", import.meta.url)))

const MAX_TIMEOUT_SECONDS = 600
const INTERRUPT_GRACE_MS = 5_000
const READY_TIMEOUT_MS = 30_000
const STDERR_TAIL_CHARS = 4_000
const RESTART_NOTICE = "[kernel restarted after unresponsive execution; state lost]"

interface DriverResponse {
  id: string
  ok: boolean
  stdout: string
  stderr: string
  result: string | null
  error: string | null
}

interface KernelConfig {
  python: string
  cwd: string
  maxOutputChars: number
  idleTTLSeconds: number
}

type InterruptReason = "timeout" | "abort"

interface ExecOutcome {
  response: DriverResponse
  interrupted: InterruptReason | null
}

/** Thrown when SIGINT failed to unstick the driver and it had to be SIGKILLed + respawned. */
class KernelRestartedError extends Error {
  constructor(readonly diagnostics: string) {
    super("kernel driver was unresponsive after SIGINT; restarted")
    this.name = "KernelRestartedError"
  }
}

function log(message: string): void {
  console.error(`[opencode-kernel] ${message}`)
}

/** One long-lived driver process with a persistent globals dict, serialized executions. */
class Kernel {
  lastUsed = Date.now()

  private child: ChildProcess | undefined
  private ready: Promise<void> = Promise.resolve()
  private readonly pending = new Map<string, { resolve: (r: DriverResponse) => void; reject: (e: Error) => void }>()
  private queue: Promise<unknown> = Promise.resolve()
  private idleTimer: NodeJS.Timeout | undefined
  private stderrTail = ""
  private counter = 0
  private disposed = false

  constructor(
    readonly sessionID: string,
    private readonly config: KernelConfig,
    private readonly onIdle: (kernel: Kernel) => void,
  ) {
    this.spawn()
    this.touch()
  }

  get diagnostics(): string {
    return this.stderrTail
  }

  /** Reset the idle-eviction timer and record recency for LRU eviction. */
  touch(): void {
    this.lastUsed = Date.now()
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => this.onIdle(this), this.config.idleTTLSeconds * 1000)
    this.idleTimer.unref?.()
  }

  /** Enqueue an execution; executions are serialized per kernel. */
  run(code: string, timeoutMs: number, signal: AbortSignal): Promise<ExecOutcome> {
    this.touch()
    const task = this.queue.then(() => this.execNow(code, timeoutMs, signal))
    this.queue = task.then(
      () => undefined,
      () => undefined,
    )
    return task
  }

  /** Kill the current driver and start a fresh one (state lost). */
  respawn(): void {
    if (this.disposed) return
    this.failAllPending(new Error("kernel restarted; previous state lost"))
    this.child?.kill("SIGKILL")
    this.spawn()
  }

  /** Permanently stop this kernel (cleanup / eviction). */
  kill(): void {
    this.disposed = true
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.failAllPending(new Error("kernel stopped"))
    this.child?.kill("SIGKILL")
    this.child = undefined
  }

  private spawn(): void {
    this.stderrTail = ""
    const child = spawn(
      this.config.python,
      ["-u", DRIVER_PATH, "--max-output", String(this.config.maxOutputChars)],
      { cwd: this.config.cwd, stdio: ["pipe", "pipe", "pipe"] },
    )
    this.child = child

    let readyResolve!: () => void
    let readyReject!: (e: Error) => void
    this.ready = new Promise<void>((resolve, reject) => {
      readyResolve = resolve
      readyReject = reject
    })
    const readyTimer = setTimeout(
      () => readyReject(new Error(`kernel driver did not become ready within ${READY_TIMEOUT_MS}ms`)),
      READY_TIMEOUT_MS,
    )
    readyTimer.unref?.()
    const settleReady = (fn: () => void) => {
      clearTimeout(readyTimer)
      fn()
    }

    child.on("error", (err: NodeJS.ErrnoException) => {
      const message =
        err.code === "ENOENT"
          ? `${this.config.python} not found — install Python 3.8+ or set the plugin's \`python\` option to an interpreter path`
          : `failed to spawn kernel driver: ${err.message}`
      const error = new Error(message)
      settleReady(() => readyReject(error))
      this.failAllPending(error)
    })

    const rl = createInterface({ input: child.stdout! })
    rl.on("line", (line) => {
      let msg: any
      try {
        msg = JSON.parse(line)
      } catch {
        log(`non-JSON line from driver: ${line.slice(0, 200)}`)
        return
      }
      if (msg && msg.ready === true) {
        settleReady(() => readyResolve())
        return
      }
      const entry = typeof msg?.id === "string" ? this.pending.get(msg.id) : undefined
      if (entry) {
        this.pending.delete(msg.id)
        entry.resolve(msg as DriverResponse)
      } else {
        log(`unmatched driver response: ${line.slice(0, 200)}`)
      }
    })

    child.stderr!.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8")
      this.stderrTail = (this.stderrTail + text).slice(-STDERR_TAIL_CHARS)
      for (const line of text.split("\n")) {
        if (line.trim()) log(`driver stderr: ${line}`)
      }
    })

    // Avoid unhandled EPIPE when the driver dies mid-write.
    child.stdin!.on("error", (err) => log(`driver stdin error: ${err.message}`))

    child.on("exit", (code, signal) => {
      const error = new Error(
        `kernel driver exited unexpectedly (code=${code}, signal=${signal})` +
          (this.stderrTail ? `\n${this.stderrTail}` : ""),
      )
      settleReady(() => readyReject(error))
      this.failAllPending(error)
      rl.close()
    })
  }

  private failAllPending(error: Error): void {
    for (const entry of this.pending.values()) entry.reject(error)
    this.pending.clear()
  }

  private async execNow(code: string, timeoutMs: number, signal: AbortSignal): Promise<ExecOutcome> {
    await this.ready
    const child = this.child
    if (!child || child.exitCode !== null || !child.stdin?.writable) {
      throw new Error("kernel driver is not running")
    }

    const id = `req-${++this.counter}`
    const response = new Promise<DriverResponse>((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
    })
    try {
      child.stdin.write(JSON.stringify({ id, code }) + "\n")
    } catch (err) {
      this.pending.delete(id)
      throw new Error(`failed to write to kernel driver: ${(err as Error).message}`)
    }

    let interrupted: InterruptReason | null = null
    let graceTimer: NodeJS.Timeout | undefined

    const interrupt = (reason: InterruptReason) => {
      if (interrupted) return
      interrupted = reason
      try {
        child.kill("SIGINT")
      } catch {
        // already dead
      }
      graceTimer = setTimeout(() => {
        // Still stuck after the grace period: hard-kill and respawn.
        const entry = this.pending.get(id)
        if (entry) {
          this.pending.delete(id)
          entry.reject(new KernelRestartedError(this.stderrTail))
        }
        try {
          child.kill("SIGKILL")
        } catch {
          // already dead
        }
        this.respawn()
      }, INTERRUPT_GRACE_MS)
      graceTimer.unref?.()
    }

    const timeoutTimer = setTimeout(() => interrupt("timeout"), timeoutMs)
    timeoutTimer.unref?.()
    const onAbort = () => interrupt("abort")
    if (signal.aborted) interrupt("abort")
    else signal.addEventListener("abort", onAbort, { once: true })

    try {
      return { response: await response, interrupted }
    } finally {
      clearTimeout(timeoutTimer)
      if (graceTimer) clearTimeout(graceTimer)
      signal.removeEventListener("abort", onAbort)
      this.touch()
    }
  }
}

function positiveNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback
}

export default Plugin.define({
  id: "opencode-kernel",
  async setup(ctx) {
    const options = ctx.options ?? {}
    const python = typeof options.python === "string" && options.python.length > 0 ? options.python : "python3"
    const timeoutSeconds = positiveNumber(options.timeoutSeconds, 120)
    const idleTTLSeconds = positiveNumber(options.idleTTLSeconds, 1800)
    const maxKernels = positiveNumber(options.maxKernels, 8)
    const maxOutputChars = positiveNumber(options.maxOutputChars, 20000)
    const permissionMode = options.permission === "allow" ? "allow" : "ask"
    const cwd = ctx.location.directory

    const kernels = new Map<string, Kernel>()

    const evict = (kernel: Kernel) => {
      if (kernels.get(kernel.sessionID) === kernel) kernels.delete(kernel.sessionID)
      kernel.kill()
    }

    const acquire = (sessionID: string, reset: boolean): Kernel => {
      let kernel = kernels.get(sessionID)
      if (kernel && reset) kernel.respawn()
      if (!kernel) {
        kernel = new Kernel(sessionID, { python, cwd, maxOutputChars, idleTTLSeconds }, evict)
        kernels.set(sessionID, kernel)
      }
      kernel.touch()
      // LRU cap: evict least-recently-used kernels beyond the cap.
      if (kernels.size > maxKernels) {
        let oldest: Kernel | undefined
        for (const other of kernels.values()) {
          if (other !== kernel && (!oldest || other.lastUsed < oldest.lastUsed)) oldest = other
        }
        if (oldest) evict(oldest)
      }
      return kernel
    }

    await ctx.tool.transform((editor) => {
      editor.add({
        name: "python",
        options: { codemode: true },
        freeform: { type: "text" },
        input: {
          type: "object",
          properties: {
            code: {
              type: "string",
              description: "Python code to execute in the persistent session kernel.",
            },
            timeout: {
              type: "number",
              description: "Optional execution timeout in seconds (default 120, capped at 600).",
            },
            reset: {
              type: "boolean",
              description: "Restart the kernel before executing, discarding all state.",
            },
          },
          required: ["code"],
          additionalProperties: false,
        },
        description:
          "Execute Python in a persistent per-session kernel. Variables, imports, functions, and loaded data survive across calls AND context compaction — the kernel process keeps running independently of the conversation. Use for multi-step, bulk, or programmatic work of any kind: file finding (pathlib/glob), parsing (ast/json/re/csv/tomllib/sqlite3), bulk multi-file transforms, running checks/tests/git via subprocess, and data/log analysis. The namespace starts empty — write your own reusable helper functions and call them again in later calls. Keep bulk data in variables and print only slices/summaries (output is truncated). Full stdlib is available, plus any third-party packages installed in the configured interpreter. Prefer native read/grep/edit for quick one-off lookups and surgical single-file edits: they keep OpenCode's diff display and per-path edit permissions, which kernel-side writes bypass. The trailing expression's value is returned as '=> <repr>'; use print() for output. Pass reset:true to discard all state; timeout is in seconds.",
        execute: async (rawInput, context) => {
          // Permission check FIRST; denial errors propagate untouched.
          await ctx.permission.assert({
            sessionID: context.sessionID,
            agent: context.agent,
            source: { type: "tool", messageID: context.messageID, id: context.id },
            action: "python",
            resources: [cwd],
            save: [cwd],
          })

          const input = rawInput as { code?: unknown; timeout?: unknown; reset?: unknown }
          if (typeof input.code !== "string" || input.code.trim().length === 0) {
            throw new Error("The python tool requires a non-empty `code` string.")
          }
          const requested =
            typeof input.timeout === "number" && Number.isFinite(input.timeout) ? input.timeout : timeoutSeconds
          const seconds = Math.min(Math.max(requested, 1), MAX_TIMEOUT_SECONDS)

          const kernel = acquire(context.sessionID, input.reset === true)
          const start = Date.now()

          let outcome: ExecOutcome
          try {
            outcome = await kernel.run(input.code, seconds * 1000, context.signal)
          } catch (err) {
            if (err instanceof KernelRestartedError) {
              const parts = [RESTART_NOTICE]
              if (err.diagnostics) parts.push(err.diagnostics)
              parts.push(`[${Date.now() - start}ms]`)
              return { content: parts.join("\n") }
            }
            throw err
          }

          const { response, interrupted } = outcome
          const parts: string[] = []
          if (interrupted === "timeout") {
            parts.push(`[execution timed out after ${seconds}s; interrupted with SIGINT]`)
          } else if (interrupted === "abort") {
            parts.push("[execution aborted]")
          }
          if (response.stdout) parts.push(response.stdout)
          if (response.stderr) parts.push(response.stderr)
          if (response.result !== null && response.result !== undefined) parts.push(`=> ${response.result}`)
          if (!response.ok && response.error) parts.push(response.error)
          parts.push(`[${Date.now() - start}ms]`)
          return { content: parts.join("\n") }
        },
      })
    })

    // Custom actions fall through to the base "*:* allow" policy, which would
    // execute Python with no prompt. Default to ask unless the user explicitly
    // allowed this action (agent config rule or saved session approval) or
    // opted out via the plugin's `permission: "allow"` option. Explicit deny
    // rules are final and never reach this hook. On any read failure we
    // escalate to ask (fail-safe).
    await ctx.permission.hook("evaluate", async (event) => {
      if (event.action !== "python" || event.effect !== "allow" || permissionMode === "allow") return
      let explicitAllow = false
      try {
        const session = await ctx.session.get({ sessionID: event.sessionID })
        const rules = [...(session.permissions ?? [])]
        if (event.agent) {
          const agent = await ctx.agent.get({ agentID: event.agent })
          rules.push(...agent.data.permissions)
        }
        const resource = event.resources[0]
        explicitAllow = rules.some(
          (r) => r.action === "python" && r.effect === "allow" && (r.resource === "*" || r.resource === resource),
        )
      } catch {
        explicitAllow = false
      }
      if (!explicitAllow) {
        event.effect = "ask"
        event.message = "opencode-kernel: execute Python in the persistent session kernel"
      }
    })

    return () => {
      for (const kernel of kernels.values()) kernel.kill()
      kernels.clear()
    }
  },
})
