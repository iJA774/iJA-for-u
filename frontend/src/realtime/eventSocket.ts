export type ConnectionState = 'connecting' | 'online' | 'offline'

export interface RuntimeEvent {
  type: string
  payload?: Record<string, unknown>
  event_seq?: number
  cursor?: string
  stream_id?: string
}

export type EventSocketFailureStage = 'connect' | 'message' | 'snapshot'

type SocketFactory = (url: string) => WebSocket
type LifecycleTarget = Pick<Window, 'addEventListener' | 'removeEventListener'>
type VisibilityTarget = Pick<Document, 'addEventListener' | 'removeEventListener' | 'visibilityState'>

export interface EventSocketOptions {
  url: string | (() => string)
  onEvent: (event: RuntimeEvent) => void
  onStateChange: (state: ConnectionState) => void
  onReconnectSnapshot: () => void | Promise<void>
  onError?: (error: unknown, stage: EventSocketFailureStage) => void
  socketFactory?: SocketFactory
  lifecycleTarget?: LifecycleTarget
  visibilityTarget?: VisibilityTarget
  random?: () => number
  baseDelayMs?: number
  maximumDelayMs?: number
  jitterRatio?: number
}

const SOCKET_CONNECTING = 0
const SOCKET_OPEN = 1

/**
 * 管理控制台事件流：断线后有界退避，页面恢复时主动补连，并在重连成功后补拉 REST 快照。
 */
export class EventSocketClient {
  private readonly socketFactory: SocketFactory
  private readonly lifecycleTarget: LifecycleTarget
  private readonly visibilityTarget: VisibilityTarget
  private readonly random: () => number
  private readonly baseDelayMs: number
  private readonly maximumDelayMs: number
  private readonly jitterRatio: number
  private socket?: WebSocket
  private reconnectTimer?: number
  private reconnectAttempt = 0
  private connectionCount = 0
  private running = false
  private snapshotPromise?: Promise<void>
  private cursor?: string
  private streamId?: string
  private lastSequence = 0

  constructor(private readonly options: EventSocketOptions) {
    this.socketFactory = options.socketFactory ?? ((url) => new WebSocket(url))
    this.lifecycleTarget = options.lifecycleTarget ?? window
    this.visibilityTarget = options.visibilityTarget ?? document
    this.random = options.random ?? Math.random
    this.baseDelayMs = options.baseDelayMs ?? 500
    this.maximumDelayMs = options.maximumDelayMs ?? 30_000
    this.jitterRatio = options.jitterRatio ?? 0.25
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.lifecycleTarget.addEventListener('online', this.handleRecovery)
    this.lifecycleTarget.addEventListener('pagehide', this.handleUnload)
    this.lifecycleTarget.addEventListener('beforeunload', this.handleUnload)
    this.visibilityTarget.addEventListener('visibilitychange', this.handleVisibility)
    this.connect()
  }

  stop(): void {
    if (!this.running) return
    this.running = false
    this.lifecycleTarget.removeEventListener('online', this.handleRecovery)
    this.lifecycleTarget.removeEventListener('pagehide', this.handleUnload)
    this.lifecycleTarget.removeEventListener('beforeunload', this.handleUnload)
    this.visibilityTarget.removeEventListener('visibilitychange', this.handleVisibility)
    this.clearReconnectTimer()
    const socket = this.socket
    this.socket = undefined
    if (socket) {
      this.detach(socket)
      socket.close()
    }
  }

  private readonly handleRecovery = (): void => {
    if (!this.running) return
    if (this.socket?.readyState === SOCKET_OPEN) {
      this.refreshSnapshot()
      return
    }
    this.reconnectNow()
  }

  private readonly handleVisibility = (): void => {
    if (this.visibilityTarget.visibilityState === 'visible') {
      this.handleRecovery()
    }
  }

  private readonly handleUnload = (): void => {
    this.stop()
  }

  private resolveUrl(): string {
    const rawUrl = typeof this.options.url === 'function' ? this.options.url() : this.options.url
    if (!this.cursor) return rawUrl
    const url = new URL(rawUrl, window.location.href)
    url.searchParams.set('cursor', this.cursor)
    return url.toString()
  }

  private connect(): void {
    if (
      !this.running
      || this.socket?.readyState === SOCKET_CONNECTING
      || this.socket?.readyState === SOCKET_OPEN
    ) {
      return
    }
    this.clearReconnectTimer()
    this.options.onStateChange('connecting')
    this.connectionCount += 1
    let socket: WebSocket
    try {
      socket = this.socketFactory(this.resolveUrl())
    } catch (reason) {
      this.options.onStateChange('offline')
      this.reportError(reason, 'connect')
      this.scheduleReconnect()
      return
    }
    this.socket = socket

    socket.onopen = () => {
      if (!this.running || this.socket !== socket) return
      const reconnected = this.connectionCount > 1
      this.reconnectAttempt = 0
      this.options.onStateChange('online')
      if (reconnected) this.refreshSnapshot()
    }
    socket.onmessage = (message) => {
      if (!this.running || this.socket !== socket) return
      const event = this.parseEvent(message.data)
      if (!event) return
      const resyncRequired = event.type === 'control.resync_required'
      const cursorAdvanced = this.advanceCursor(event)
      // resync_required 是终止控制帧，可能与已处理的最后一条事件共用序号；
      // 即使游标去重拒绝它，也必须立刻补拉权威快照。
      if (resyncRequired) {
        this.refreshSnapshot()
      }
      if (!cursorAdvanced && !resyncRequired) return
      try {
        this.options.onEvent(event)
      } catch (reason) {
        this.reportError(reason, 'message')
      }
    }
    socket.onerror = () => this.disconnect(socket)
    socket.onclose = () => this.disconnect(socket)
  }

  private disconnect(socket: WebSocket): void {
    if (!this.running || this.socket !== socket) return
    this.socket = undefined
    this.detach(socket)
    this.options.onStateChange('offline')
    if (socket.readyState === SOCKET_CONNECTING || socket.readyState === SOCKET_OPEN) {
      socket.close()
    }
    this.scheduleReconnect()
  }

  private detach(socket: WebSocket): void {
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
  }

  private scheduleReconnect(): void {
    if (!this.running || this.reconnectTimer !== undefined) return
    const exponentialDelay = Math.min(
      this.maximumDelayMs,
      this.baseDelayMs * 2 ** this.reconnectAttempt,
    )
    const jitter = 1 + (this.random() * 2 - 1) * this.jitterRatio
    const delay = Math.max(0, Math.round(exponentialDelay * jitter))
    this.reconnectAttempt += 1
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = undefined
      this.connect()
    }, delay)
  }

  private reconnectNow(): void {
    this.clearReconnectTimer()
    const socket = this.socket
    this.socket = undefined
    if (socket) {
      this.detach(socket)
      socket.close()
    }
    this.connect()
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === undefined) return
    window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = undefined
  }

  private refreshSnapshot(): void {
    if (this.snapshotPromise) return
    this.snapshotPromise = Promise.resolve()
      .then(() => this.options.onReconnectSnapshot())
      .catch(reason => this.reportError(reason, 'snapshot'))
      .finally(() => {
        this.snapshotPromise = undefined
      })
  }

  private parseEvent(raw: unknown): RuntimeEvent | undefined {
    if (typeof raw !== 'string') {
      this.reportError(new Error('事件消息必须是 JSON 文本'), 'message')
      return undefined
    }
    try {
      const parsed = JSON.parse(raw) as {
        type?: unknown
        payload?: unknown
        event_seq?: unknown
        cursor?: unknown
        stream_id?: unknown
      }
      if (typeof parsed.type !== 'string') {
        this.reportError(new Error('事件消息缺少字符串 type'), 'message')
        return undefined
      }
      const hasSequenceMetadata = (
        parsed.event_seq !== undefined
        || parsed.cursor !== undefined
        || parsed.stream_id !== undefined
      )
      if (
        hasSequenceMetadata
        && (
          !Number.isSafeInteger(parsed.event_seq)
          || (parsed.event_seq as number) < 0
          || typeof parsed.cursor !== 'string'
          || typeof parsed.stream_id !== 'string'
        )
      ) {
        this.reportError(new Error('事件消息的序号或游标无效'), 'message')
        return undefined
      }
      return {
        type: parsed.type,
        payload: parsed.payload && typeof parsed.payload === 'object'
          ? parsed.payload as Record<string, unknown>
          : undefined,
        event_seq: hasSequenceMetadata ? parsed.event_seq as number : undefined,
        cursor: hasSequenceMetadata ? parsed.cursor as string : undefined,
        stream_id: hasSequenceMetadata ? parsed.stream_id as string : undefined,
      }
    } catch {
      this.reportError(new Error('事件消息不是有效 JSON'), 'message')
      return undefined
    }
  }

  private advanceCursor(event: RuntimeEvent): boolean {
    if (
      event.event_seq === undefined
      || event.cursor === undefined
      || event.stream_id === undefined
    ) {
      return true
    }
    if (this.streamId === event.stream_id && event.event_seq <= this.lastSequence) {
      return false
    }
    if (this.streamId !== undefined && this.streamId !== event.stream_id) {
      this.refreshSnapshot()
    }
    this.streamId = event.stream_id
    this.lastSequence = event.event_seq
    this.cursor = event.cursor
    return true
  }

  private reportError(error: unknown, stage: EventSocketFailureStage): void {
    if (this.options.onError) {
      this.options.onError(error, stage)
      return
    }
    console.error(`实时事件流在 ${stage} 阶段失败`, error)
  }
}

export const RUNTIME_EVENT_NAME = 'ija:event'
export const SNAPSHOT_EVENT_NAME = 'ija:snapshot'
