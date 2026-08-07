import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { EventSocketClient } from './eventSocket'

class FakeSocket {
  readyState: number = WebSocket.CONNECTING
  onopen: WebSocket['onopen'] = null
  onmessage: WebSocket['onmessage'] = null
  onerror: WebSocket['onerror'] = null
  onclose: WebSocket['onclose'] = null
  close = vi.fn(() => {
    this.readyState = WebSocket.CLOSED
  })

  open() {
    this.readyState = WebSocket.OPEN
    this.onopen?.call(this as unknown as WebSocket, new Event('open'))
  }

  disconnect() {
    this.readyState = WebSocket.CLOSED
    this.onclose?.call(this as unknown as WebSocket, new CloseEvent('close'))
  }

  message(data: string) {
    this.onmessage?.call(
      this as unknown as WebSocket,
      new MessageEvent('message', { data }),
    )
  }
}

class FakeVisibilityTarget extends EventTarget {
  visibilityState: DocumentVisibilityState = 'visible'
}

function createHarness() {
  const sockets: FakeSocket[] = []
  const urls: string[] = []
  const lifecycle = new EventTarget()
  const visibility = new FakeVisibilityTarget()
  const onEvent = vi.fn()
  const onStateChange = vi.fn()
  const onReconnectSnapshot = vi.fn(async () => undefined)
  const onError = vi.fn()
  const client = new EventSocketClient({
    url: 'ws://localhost/api/events',
    socketFactory: (url) => {
      urls.push(url)
      const socket = new FakeSocket()
      sockets.push(socket)
      return socket as unknown as WebSocket
    },
    lifecycleTarget: lifecycle as unknown as Window,
    visibilityTarget: visibility as unknown as Document,
    random: () => 0.5,
    baseDelayMs: 100,
    maximumDelayMs: 1_000,
    jitterRatio: 0.25,
    onEvent,
    onStateChange,
    onReconnectSnapshot,
    onError,
  })
  return {
    client,
    lifecycle,
    visibility,
    sockets,
    urls,
    onEvent,
    onStateChange,
    onReconnectSnapshot,
    onError,
  }
}

describe('EventSocketClient', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('断线后按指数退避重连并应用可控抖动', () => {
    const harness = createHarness()
    harness.client.start()
    expect(harness.sockets).toHaveLength(1)

    harness.sockets[0].disconnect()
    vi.advanceTimersByTime(99)
    expect(harness.sockets).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(harness.sockets).toHaveLength(2)

    harness.sockets[1].disconnect()
    vi.advanceTimersByTime(199)
    expect(harness.sockets).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(harness.sockets).toHaveLength(3)
    expect(harness.onStateChange).toHaveBeenLastCalledWith('connecting')

    harness.client.stop()
  })

  it('重连成功后补拉 REST 快照，首次连接不重复补拉', async () => {
    const harness = createHarness()
    harness.client.start()
    harness.sockets[0].open()
    expect(harness.onReconnectSnapshot).not.toHaveBeenCalled()

    harness.sockets[0].disconnect()
    vi.advanceTimersByTime(100)
    harness.sockets[1].open()
    await Promise.resolve()

    expect(harness.onReconnectSnapshot).toHaveBeenCalledTimes(1)
    expect(harness.onStateChange).toHaveBeenLastCalledWith('online')
    harness.client.stop()
  })

  it('online 与页面重新可见时立即恢复，卸载后彻底停止', async () => {
    const harness = createHarness()
    harness.client.start()
    harness.sockets[0].disconnect()

    harness.lifecycle.dispatchEvent(new Event('online'))
    expect(harness.sockets).toHaveLength(2)
    vi.advanceTimersByTime(1_000)
    expect(harness.sockets).toHaveLength(2)

    harness.sockets[1].open()
    await vi.runAllTimersAsync()
    harness.visibility.visibilityState = 'hidden'
    harness.visibility.dispatchEvent(new Event('visibilitychange'))
    expect(harness.onReconnectSnapshot).toHaveBeenCalledTimes(1)

    harness.visibility.visibilityState = 'visible'
    harness.visibility.dispatchEvent(new Event('visibilitychange'))
    await Promise.resolve()
    expect(harness.onReconnectSnapshot).toHaveBeenCalledTimes(2)

    harness.lifecycle.dispatchEvent(new Event('pagehide'))
    expect(harness.sockets[1].close).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(10_000)
    expect(harness.sockets).toHaveLength(2)
  })

  it('仅分发结构正确的 JSON 事件', () => {
    const harness = createHarness()
    harness.client.start()
    harness.sockets[0].open()

    harness.sockets[0].message('not-json')
    harness.sockets[0].message('{"payload":{}}')
    harness.sockets[0].message('{"type":"session.updated","payload":{"session_id":"s1"}}')

    expect(harness.onEvent).toHaveBeenCalledTimes(1)
    expect(harness.onError).toHaveBeenCalledTimes(2)
    expect(harness.onEvent).toHaveBeenCalledWith({
      type: 'session.updated',
      payload: { session_id: 's1' },
    })
    harness.client.stop()
  })

  it('保存单调游标，并在断线重连时请求缺口回放', () => {
    const harness = createHarness()
    harness.client.start()
    harness.sockets[0].open()
    harness.sockets[0].message(JSON.stringify({
      type: 'session.updated',
      payload: { session_id: 's1' },
      event_seq: 7,
      cursor: 'stream-a.7',
      stream_id: 'stream-a',
    }))
    // 相同序号属于回放与实时订阅交界处的重复帧，不应二次分发。
    harness.sockets[0].message(JSON.stringify({
      type: 'session.updated',
      payload: { session_id: 's1' },
      event_seq: 7,
      cursor: 'stream-a.7',
      stream_id: 'stream-a',
    }))
    harness.sockets[0].disconnect()
    vi.advanceTimersByTime(100)

    expect(harness.onEvent).toHaveBeenCalledTimes(1)
    expect(new URL(harness.urls[1]).searchParams.get('cursor')).toBe('stream-a.7')
    harness.client.stop()
  })

  it('收到重同步控制帧时立即补拉 REST 快照', async () => {
    const harness = createHarness()
    harness.client.start()
    harness.sockets[0].open()
    harness.sockets[0].message(JSON.stringify({
      type: 'control.resync_required',
      payload: { reason: 'history_expired', snapshot_required: true },
      event_seq: 12,
      cursor: 'stream-b.12',
      stream_id: 'stream-b',
    }))
    await Promise.resolve()

    expect(harness.onReconnectSnapshot).toHaveBeenCalledTimes(1)
    expect(harness.onEvent).toHaveBeenCalledWith(expect.objectContaining({
      type: 'control.resync_required',
      cursor: 'stream-b.12',
    }))
    harness.client.stop()
  })

  it('重同步帧与最后事件同序号时仍强制补拉快照', async () => {
    const harness = createHarness()
    harness.client.start()
    harness.sockets[0].open()
    harness.sockets[0].message(JSON.stringify({
      type: 'session.updated',
      payload: { session_id: 's1' },
      event_seq: 12,
      cursor: 'stream-b.12',
      stream_id: 'stream-b',
    }))
    harness.sockets[0].message(JSON.stringify({
      type: 'control.resync_required',
      payload: { reason: 'slow_consumer', snapshot_required: true },
      event_seq: 12,
      cursor: 'stream-b.12',
      stream_id: 'stream-b',
    }))
    await Promise.resolve()

    expect(harness.onReconnectSnapshot).toHaveBeenCalledTimes(1)
    expect(harness.onEvent).toHaveBeenCalledTimes(2)
    expect(harness.onEvent).toHaveBeenLastCalledWith(expect.objectContaining({
      type: 'control.resync_required',
      cursor: 'stream-b.12',
    }))
    harness.client.stop()
  })
})
