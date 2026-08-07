import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, bootstrapControlSession } from './client'

describe('bootstrapControlSession', () => {
  afterEach(() => {
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
    vi.unstubAllGlobals()
  })

  it('在首个请求前消费并清除本机登录链接中的 fragment Token', async () => {
    const token = 'fragment-control-token-for-test'
    window.history.replaceState(
      { test: true },
      '',
      `/?source=maintenance#control_token=${token}&view=settings`,
    )
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ ok: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await bootstrapControlSession()

    expect(window.location.href).not.toContain(token)
    expect(window.location.search).toBe('?source=maintenance')
    expect(window.location.hash).toBe('#view=settings')
    expect(fetchMock).toHaveBeenCalledWith('/api/control/session', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Authorization: `Bearer ${token}` },
    })
  })

  it('只通过 Authorization 交接 Token，并立即清除 JS 可读副本', async () => {
    const token = 'browser-control-token-for-test'
    window.sessionStorage.setItem('ija.control_token', token)
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ ok: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await bootstrapControlSession()

    expect(fetchMock).toHaveBeenCalledWith('/api/control/session', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(window.sessionStorage.getItem('ija.control_token')).toBeNull()
  })

  it('认证失败也不保留 Token，且只暴露服务端通用错误', async () => {
    window.sessionStorage.setItem('ija.control_token', 'wrong-secret-token')
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({
        error: {
          code: 'control_authentication_required',
          message: '控制面认证失败',
        },
      }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(bootstrapControlSession()).rejects.toMatchObject({
      message: '控制面认证失败',
      code: 'control_authentication_required',
    })
    expect(window.sessionStorage.getItem('ija.control_token')).toBeNull()
  })
})

describe('api.searchMessages', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('编码 Session ID 和搜索文本中的特殊字符', async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify([]),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      },
    ))
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.searchMessages(
      'session/with space',
      '阿澄 & 火锅?',
      25,
    )

    expect(result).toEqual([])
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/session%2Fwith%20space/messages/search?query=%E9%98%BF%E6%BE%84%20%26%20%E7%81%AB%E9%94%85%3F&limit=25',
      {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      },
    )
  })

  it('编码消息上下文请求中的两个路径 ID', async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify([]),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      },
    ))
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.messageContext(
      'session/with space',
      'message/old one?',
      20,
      30,
    )

    expect(result).toEqual([])
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/session%2Fwith%20space/messages/message%2Fold%20one%3F/context?before=20&after=30',
      {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      },
    )
  })
})
