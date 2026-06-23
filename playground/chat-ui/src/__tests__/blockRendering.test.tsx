/**
 * SSE injection: `vi.spyOn(gatewayClient, 'consumeSseJson')` drives the same `handleGatewayEvent`
 * path `App` wires in production (bytes are still parsed by the real `consumeSseJson` in prod).
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import UnknownEventFallback from '../blocks/UnknownEventFallback'
import SystemNotificationToast from '../blocks/SystemNotificationToast'
import * as gw from '../gatewayClient'
import type { GatewayJsonEvent } from '../gatewayClient'
import { frontendToolRegistry } from '../frontendToolRegistry'

const PNG_1PX =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

const SSE_END = Symbol('SSE_END')

class SseTestFeed {
  private readonly buf: GatewayJsonEvent[] = []
  private readonly waiters: Array<(v: GatewayJsonEvent | typeof SSE_END) => void> = []
  private ended = false

  emit(ev: GatewayJsonEvent): void {
    const w = this.waiters.shift()
    if (w) w(ev)
    else this.buf.push(ev)
  }

  finish(): void {
    this.ended = true
    for (const w of this.waiters.splice(0)) w(SSE_END)
  }

  private async shift(signal?: AbortSignal): Promise<GatewayJsonEvent | typeof SSE_END> {
    const head = this.buf.shift()
    if (head !== undefined) return head
    if (this.ended || signal?.aborted) return SSE_END
    return await new Promise((resolve) => {
      const resolver = (v: GatewayJsonEvent | typeof SSE_END) => resolve(v)
      this.waiters.push(resolver)
      const onAbort = () => {
        const i = this.waiters.indexOf(resolver as never)
        if (i !== -1) this.waiters.splice(i, 1)
        resolve(SSE_END)
      }
      signal?.addEventListener('abort', onAbort, { once: true })
    })
  }

  consumeLoop(onEvent: (e: GatewayJsonEvent) => void, signal?: AbortSignal): Promise<void> {
    return (async () => {
      while (true) {
        const ev = await this.shift(signal)
        if (ev === SSE_END) break
        onEvent(ev)
      }
    })()
  }
}

const usageFixture: gw.SessionUsageResponse = {
  session_id: 'sid-test',
  turns: 0,
  input_tokens: 0,
  output_tokens: 0,
  cached_tokens: 0,
  cost_usd: 0,
  period_start: 0,
  period_end: 0,
  last_prompt_tokens: 0,
  estimated_prompt_tokens: 0,
  summarization_threshold_tokens: 850_000,
  context_window_tokens: 1_000_000,
}

let sseFeed: SseTestFeed
let fetchMock: ReturnType<typeof vi.fn>

function installGatewaySpies(feed: SseTestFeed): void {
  vi.spyOn(gw, 'createSession').mockResolvedValue({ session_id: 'sid-test', created_at: 0 })
  vi.spyOn(gw, 'openEventsStream').mockResolvedValue(
    new Response(new ReadableStream(), { status: 200, headers: { 'content-type': 'text/event-stream' } }),
  )
  vi.spyOn(gw, 'fetchSessionUsage').mockResolvedValue(usageFixture)
  vi.spyOn(gw, 'consumeSseJson').mockImplementation((_res, onEvent, signal) =>
    feed.consumeLoop(onEvent, signal),
  )
}

function defaultFetchResponder(input: RequestInfo | URL, init?: RequestInit): Response | null {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method ?? 'GET').toUpperCase()

  if (url.endsWith('/sessions') && method === 'POST' && !url.includes('sid-test')) {
    return new Response(JSON.stringify({ session_id: 'sid-test', created_at: 0 }), { status: 201 })
  }
  if (url.includes('/sessions/sid-test/usage') && method === 'GET') {
    return new Response(JSON.stringify(usageFixture), { status: 200 })
  }
  if (url.includes('/sessions/sid-test/events') && method === 'GET') {
    return new Response(new ReadableStream(), { status: 200 })
  }
  if (url.includes('/sessions/sid-test/reply') && method === 'POST') {
    return new Response(null, { status: 200 })
  }
  if (url.includes('/sessions/sid-test/attachments') && method === 'POST') {
    return new Response(
      JSON.stringify({
        attachment_id: 'att_test1',
        mime_type: 'image/png',
        size_bytes: 100,
        filename: 'dot.png',
        created_at: Date.now(),
      }),
      { status: 201, headers: { 'content-type': 'application/json' } },
    )
  }
  if (url.includes('/sessions/sid-test/cancel') && method === 'POST') {
    return new Response(null, { status: 200 })
  }
  return null
}

beforeEach(() => {
  sseFeed = new SseTestFeed()
  fetchMock = vi.fn()
  globalThis.fetch = fetchMock as typeof fetch

  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const d = defaultFetchResponder(input, init)
    if (d) return Promise.resolve(d)
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    return Promise.resolve(new Response(`unexpected fetch mock: ${method} ${url}`, { status: 500 }))
  })

  installGatewaySpies(sseFeed)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  for (const k of Object.keys(frontendToolRegistry)) {
    delete frontendToolRegistry[k]
  }
})

async function renderConnected(): Promise<void> {
  const userEv = userEvent.setup()
  render(<App />)
  await userEv.click(screen.getByRole('button', { name: /new session/i }))
  await waitFor(() => expect(screen.getByText(/^connected/i)).toBeInTheDocument())
}

describe('gatewayClient POST helpers (unit)', () => {
  it('postToolConfirmation_posts_expected_body_and_url', async () => {
    fetchMock.mockImplementationOnce(async (input, init) => {
      expect(String(input)).toContain(`/sessions/sx/tool-confirmations/call-1`)
      expect(init?.method).toBe('POST')
      expect(init?.body).toBe(JSON.stringify({ approved: true }))
      return new Response(JSON.stringify({ ok: true }), { status: 202 })
    })

    await gw.postToolConfirmation('sx', 'call-1', { approved: true })
  })

  it('post_elicitation_posts_user_data_shape', async () => {
    fetchMock.mockImplementationOnce(async (_, init) => {
      expect(init?.body).toBe(JSON.stringify({ user_data: { a: 1 } }))
      return new Response(JSON.stringify({ ok: true }), { status: 202 })
    })

    await gw.postElicitation('sx', 'el1', { user_data: { a: 1 } })
  })

  it('post_frontend_tool_result_posts_result_and_is_error', async () => {
    fetchMock.mockImplementationOnce(async (_, init) => {
      expect(JSON.parse(String(init?.body))).toEqual({
        result: [{ type: 'text', text: 'x' }],
        is_error: false,
      })
      return new Response(JSON.stringify({ ok: true }), { status: 202 })
    })

    await gw.postFrontendToolResult('sx', 'ft1', {
      result: [{ type: 'text', text: 'x' }],
      is_error: false,
    })
  })

  it('test_create_session_sends_model_body', async () => {
    vi.mocked(gw.createSession).mockRestore()
    fetchMock.mockImplementationOnce(async (input, init) => {
      expect(String(input)).toMatch(/\/sessions$/)
      expect(init?.method).toBe('POST')
      expect(JSON.parse(String(init?.body))).toEqual({
        model_provider: 'openai',
        model_name: 'gpt-5',
      })
      return new Response(JSON.stringify({ session_id: 's1', created_at: 0 }), { status: 201 })
    })

    await gw.createSession(undefined, { model_provider: 'openai', model_name: 'gpt-5' })
  })

  it('test_create_session_empty_body_when_no_opts', async () => {
    vi.mocked(gw.createSession).mockRestore()
    fetchMock.mockImplementationOnce(async (input, init) => {
      expect(String(input)).toMatch(/\/sessions$/)
      expect(init?.method).toBe('POST')
      expect(init?.body).toBe('{}')
      return new Response(JSON.stringify({ session_id: 's1', created_at: 0 }), { status: 201 })
    })

    await gw.createSession()
  })

  it('postAttachment_posts_multipart_file', async () => {
    fetchMock.mockImplementationOnce(async (input, init) => {
      expect(String(input)).toContain('/sessions/sx/attachments')
      expect(init?.method).toBe('POST')
      expect(init?.body).toBeInstanceOf(FormData)
      return new Response(
        JSON.stringify({
          attachment_id: 'att_x',
          mime_type: 'image/png',
          size_bytes: 4,
          filename: 'a.png',
          created_at: 1,
        }),
        { status: 201, headers: { 'content-type': 'application/json' } },
      )
    })
    const file = new File([new Uint8Array([1, 2, 3, 4])], 'a.png', { type: 'image/png' })
    const res = await gw.postAttachment('sx', file)
    expect(res.attachment_id).toBe('att_x')
  })

  it('postReply_sends_content_blocks', async () => {
    fetchMock.mockImplementationOnce(async (_input, init) => {
      expect(JSON.parse(String(init?.body))).toEqual({
        request_id: 'r1',
        content: [
          { type: 'text', text: 'hi' },
          { type: 'attachmentRef', attachmentId: 'att_1', mimeType: 'image/png' },
        ],
      })
      return new Response(null, { status: 200 })
    })
    await gw.postReply('s1', 'r1', [
      { type: 'text', text: 'hi' },
      { type: 'attachmentRef', attachmentId: 'att_1', mimeType: 'image/png' },
    ])
  })
})

describe('model selector (App)', () => {
  it('test_selector_renders_three_options', () => {
    render(<App />)
    const select = screen.getByRole('combobox', { name: /model/i })
    const options = within(select).getAllByRole('option')
    expect(options).toHaveLength(3)
    expect(options.map((o) => o.textContent)).toEqual([
      'OpenAI',
      'Vertex Gemini',
      'Anthropic (Vertex)',
    ])
  })

  it('test_connect_passes_selected_model', async () => {
    const createSpy = vi.spyOn(gw, 'createSession').mockResolvedValue({
      session_id: 'sid-model',
      created_at: 0,
    })
    vi.spyOn(gw, 'openEventsStream').mockResolvedValue(
      new Response(new ReadableStream(), { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    )
    vi.spyOn(gw, 'fetchSessionUsage').mockResolvedValue(usageFixture)
    vi.spyOn(gw, 'consumeSseJson').mockImplementation(() => Promise.resolve())

    const userEv = userEvent.setup()
    render(<App />)

    const select = screen.getByRole('combobox', { name: /model/i })
    await userEv.selectOptions(select, 'Vertex Gemini')
    await userEv.click(screen.getByRole('button', { name: /new session/i }))

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith(undefined, {
        model_provider: 'gemini',
        model_name: 'gemini-3-flash-preview',
      })
    })
  })

  it('test_model_unavailable_shows_note', async () => {
    vi.spyOn(gw, 'createSession').mockRejectedValue(
      new Error('createSession failed: 400 {"error":{"code":"MODEL_UNAVAILABLE","message":"Model provider openai unavailable"}}'),
    )

    const userEv = userEvent.setup()
    render(<App />)
    await userEv.click(screen.getByRole('button', { name: /new session/i }))

    await waitFor(() => {
      expect(
        screen.getByText(/selected model unavailable.*check credentials or pick another/i),
      ).toBeInTheDocument()
    })
    expect(screen.queryByText(/^connected/i)).not.toBeInTheDocument()
  })
})

describe('blockRendering integration', () => {
  it('test_image_block_bubble_renders_data_uri_png', async () => {
    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ImageBlock',
        request_id: 'img1',
        mime_type: 'image/png',
        data: PNG_1PX,
      })
    })

    await waitFor(() => {
      const img = screen.getByRole('img', { name: /assistant image/i })
      expect(img.getAttribute('src')).toMatch(/^data:image\/png;base64,/)
      expect(img.getAttribute('src')).toContain(PNG_1PX)
    })

    const wrap = await screen.findByTestId('image-block-bubble')
    within(wrap).getByRole('img')
    expect(wrap.className).toContain('image-block-inline')
  })

  it('test_tool_invocation_merges_started_and_result_into_one_card_with_pretty_json', async () => {
    await renderConnected()
    const rid = 'tool-req-1'
    await act(async () => {
      sseFeed.emit({
        type: 'ToolCallStarted',
        request_id: rid,
        tool: 'run_command',
        label: 'run_command',
        args: { command: 'ls' },
      })
    })

    const cards = await screen.findAllByTestId('tool-invocation-card')
    expect(cards).toHaveLength(1)
    expect(screen.getByText('run_command')).toBeInTheDocument()
    expect(screen.getByText(/^Running$/i)).toBeInTheDocument()

    await act(async () => {
      sseFeed.emit({
        type: 'ToolCallResult',
        request_id: rid,
        tool: 'run_command',
        result: JSON.stringify({ ok: true, stdout: 'a\nb', stderr: '', exit_code: 0 }),
      })
    })

    await waitFor(() => {
      expect(screen.getByText(/^Done$/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/^Running$/i)).not.toBeInTheDocument()
    expect(screen.getAllByTestId('tool-invocation-card')).toHaveLength(1)

    const card = screen.getByTestId('tool-invocation-card')
    const details = card.querySelector('details')
    expect(details).not.toBeNull()
    expect(details).not.toHaveAttribute('open')
    expect(within(card).getByText('ls')).toBeInTheDocument()

    const summary = card.querySelector('summary')
    expect(summary).not.toBeNull()
    fireEvent.click(summary!)
    expect(details).toHaveAttribute('open')

    expect(card.textContent).toContain('"command"')
    expect(card.textContent).toContain('"stdout"')
    expect(card.textContent).toContain('a')
  })

  it('test_thinking_complete_collapses_to_one_line_summary_click_re_expands', async () => {
    await renderConnected()
    const rid = 't-req'

    await act(async () => {
      sseFeed.emit({ type: 'ThinkingBlockDelta', request_id: rid, text: 'step 1\n' })
      sseFeed.emit({ type: 'ThinkingBlockDelta', request_id: rid, text: 'step 2' })
    })

    const region = await screen.findByRole('region', { name: /thinking/i })
    expect(region.textContent).toContain('step 2')

    await act(async () => {
      sseFeed.emit({ type: 'ThinkingBlockComplete', request_id: rid, signature: '' })
    })

    await waitFor(() => {
      const r = screen.getByRole('region', { name: /thinking/i })
      expect(r.textContent ?? '').not.toContain('step 2')
    })

    const collapsed = screen.getByRole('region', { name: /thinking/i })
    const line = (collapsed.textContent ?? '')
      .split('\n')
      .map((s) => s.trim())
      .find((s) => s.length > 0) ?? ''
    expect(line.length).toBeLessThanOrEqual(120)
    expect(line).toContain('step 1')

    await userEvent.click(collapsed)

    await waitFor(() => {
      expect(screen.getByRole('region', { name: /thinking/i }).textContent).toContain('step 2')
    })
  })

  it('test_redacted_thinking_no_expand_affordance', async () => {
    await renderConnected()
    await act(async () => {
      sseFeed.emit({ type: 'RedactedThinkingBlock', request_id: 'r1', data: 'opaque' })
    })

    await waitFor(() => {
      expect(screen.getByTestId('redacted-thinking-placeholder')).toHaveTextContent(/\[redacted thinking\]/i)
    })
    expect(document.querySelector('.redacted-thinking details')).toBeNull()
  })

  it('test_tool_confirmation_approve_posts_approved_true_202_dismisses', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/tool-confirmations/tc_1') && method === 'POST') {
        expect(JSON.parse(String(init?.body))).toEqual({ approved: true })
        return new Response(JSON.stringify({ ok: true }), { status: 202 })
      }
      return Promise.resolve(new Response(`fallback ${method} ${url}`, { status: 500 }))
    })

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ToolConfirmationRequest',
        tool_call_id: 'tc_1',
        tool_name: 'read_file',
        arguments: { path: '/x' },
        prompt: 'Allow?',
      })
    })

    await waitFor(() => screen.getByTestId('tool-confirmation-modal'))

    expect(screen.getByRole('heading', { name: /read_file/i })).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: /tool confirmation/i })).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: /tool confirmation/i }).textContent).toContain('/x')
    expect(screen.getByText(/Allow\?/)).toBeInTheDocument()

    const userEv = userEvent.setup()
    await userEv.click(screen.getByRole('button', { name: /^Approve$/i }))

    expect(
      fetchMock.mock.calls.filter(
        ([u, cfg]) => String(u).includes('tool-confirmations/tc_1') && (cfg as RequestInit)?.method === 'POST',
      ),
    ).not.toHaveLength(0)

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /tool confirmation/i })).not.toBeInTheDocument(),
    )
  })

  it('test_tool_confirmation_deny_posts_approved_false_optional_reason', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/tool-confirmations/tc_1') && method === 'POST') {
        expect(JSON.parse(String(init?.body)).approved).toBe(false)
        return new Response(JSON.stringify({ ok: true }), { status: 202 })
      }
      return Promise.resolve(new Response(`fallback ${method} ${url}`, { status: 500 }))
    })

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ToolConfirmationRequest',
        tool_call_id: 'tc_1',
        tool_name: 'read_file',
        arguments: {},
        prompt: 'Allow?',
      })
    })

    await waitFor(() => screen.getByRole('button', { name: /^Deny$/i }))
    await userEvent.click(screen.getByRole('button', { name: /^Deny$/i }))

    expect(
      fetchMock.mock.calls.filter(
        ([u, cfg]) => String(u).includes('tool-confirmations/tc_1') && (cfg as RequestInit)?.method === 'POST',
      ),
    ).not.toHaveLength(0)

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /tool confirmation/i })).not.toBeInTheDocument(),
    )
  })

  it('test_tool_confirmation_posts_error_keeps_modal_open_on_409', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/tool-confirmations/tc_409') && method === 'POST') {
        return new Response(JSON.stringify({ error: { code: 'conflict', message: 'nope', request_id: 'x' } }), {
          status: 409,
        })
      }
      return Promise.resolve(new Response(`fallback ${method} ${url}`, { status: 500 }))
    })

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ToolConfirmationRequest',
        tool_call_id: 'tc_409',
        tool_name: 'read_file',
        arguments: {},
      })
    })

    await waitFor(() => screen.getByTestId('tool-confirmation-modal'))

    await userEvent.click(screen.getByRole('button', { name: /^Approve$/i }))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toMatch(/409|conflict|nope/i)
    })
    expect(screen.getByRole('dialog', { name: /tool confirmation/i })).toBeInTheDocument()
  })

  it('test_elicitation_submits_choice_yes_via_user_data', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)

      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/elicitations/el_1') && method === 'POST') {
        expect(JSON.parse(String(init?.body))).toEqual({ user_data: { choice: 'yes' } })
        return new Response(JSON.stringify({ ok: true }), { status: 202 })
      }
      return Promise.resolve(new Response(`fallback ${method} ${url}`, { status: 500 }))
    })

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ActionRequiredEvent',
        action_type: 'elicitation',
        id: 'el_1',
        payload: {
          message: 'Pick one',
          requested_schema: {
            type: 'object',
            properties: { choice: { type: 'string' } },
            required: ['choice'],
          },
        },
      })
    })

    await waitFor(() => screen.getByRole('textbox', { name: /^choice$/i }))
    await userEvent.type(screen.getByRole('textbox', { name: /^choice$/i }), 'yes')
    await userEvent.click(screen.getByRole('button', { name: /^Submit$/i }))

    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/elicitations/el_1'))).toBe(true)

    await waitFor(() => expect(screen.queryByRole('dialog', { name: /elicitation/i })).not.toBeInTheDocument())
  })

  it('test_elicitation_reject_posts_user_data_null', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/elicitations/el_1') && method === 'POST') {
        expect(JSON.parse(String(init?.body))).toEqual({ user_data: null })
        return new Response(JSON.stringify({ ok: true }), { status: 202 })
      }
      return Promise.resolve(new Response(`fallback ${method} ${url}`, { status: 500 }))
    })

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ActionRequiredEvent',
        action_type: 'elicitation',
        id: 'el_1',
        payload: {
          message: 'Pick one',
          requested_schema: {
            type: 'object',
            properties: { choice: { type: 'string' } },
            required: ['choice'],
          },
        },
      })
    })

    await waitFor(() => screen.getByTestId('elicitation-form'))
    await userEvent.click(screen.getByRole('button', { name: /^Reject$/i }))

    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/elicitations/el_1'))).toBe(true)

    await waitFor(() => expect(screen.queryByRole('dialog', { name: /elicitation/i })).not.toBeInTheDocument())
  })

  it('test_frontend_tool_echo_registry_and_posts_result', async () => {
    frontendToolRegistry.echo = async () => ({
      result: [{ type: 'text', text: 'hello' }],
      is_error: false,
    })

    const postSpy = vi.spyOn(gw, 'postFrontendToolResult').mockResolvedValue(undefined as never)

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'FrontendToolRequest',
        tool_call_id: 'fe_1',
        name: 'echo',
        args: { msg: 'hi' },
      })
    })

    await waitFor(() => expect(postSpy).toHaveBeenCalled())

    expect(postSpy.mock.calls.some(([, , body]) => (body as { is_error?: boolean }).is_error === false)).toBe(true)
    expect(JSON.stringify(postSpy.mock.calls)).toContain('hello')
  })

  it('test_frontend_unknown_handler_posts_error_text', async () => {
    const postSpy = vi.spyOn(gw, 'postFrontendToolResult').mockResolvedValue(undefined as never)

    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'FrontendToolRequest',
        tool_call_id: 'fe_x',
        name: 'nope',
        args: {},
      })
    })

    await waitFor(() => expect(postSpy).toHaveBeenCalled())

    expect(postSpy.mock.calls.some(([, , body]) => (body as { is_error?: boolean }).is_error === true)).toBe(true)
    const joined = JSON.stringify(postSpy.mock.calls)
    expect(joined.toLowerCase()).toContain('no frontend handler')
  })

  it('test_system_notification_toast_manual_dismiss', async () => {
    await renderConnected()

    await act(async () => {
      sseFeed.emit({
        type: 'SystemNotificationEvent',
        notification_type: 'inlineMessage',
        msg: 'hi',
      })
    })

    await waitFor(() => expect(screen.getByText('hi')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByText('hi')).not.toBeInTheDocument())
  })

  it('test_system_notification_autodismiss_fake_timers', () => {
    vi.useFakeTimers()
    const onDismiss = vi.fn()
    render(
      <SystemNotificationToast
        toasts={[{ id: 't1', notification_type: 'inlineMessage', msg: 'auto-go' }]}
        onDismiss={onDismiss}
      />,
    )
    expect(screen.getByText('auto-go')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(5000)
    })
    expect(onDismiss).toHaveBeenCalledWith('t1')
  })

  it('test_unknown_event_type_swallowed_then_image_renders', async () => {
    await renderConnected()
    await act(async () => {
      sseFeed.emit({ type: 'someBrandNewEvent', ping: true } as unknown as GatewayJsonEvent)
      sseFeed.emit({ type: 'ImageBlock', mime_type: 'image/png', data: PNG_1PX, request_id: 'i2' })
    })

    expect(screen.queryByTestId('unknown-event-fallback')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByRole('img').length).toBeGreaterThan(0))
  })

  it('test_stop_clears_tool_confirmation_via_post_cancel', async () => {
    const userEv = userEvent.setup()
    render(<App />)
    await userEv.click(screen.getByRole('button', { name: /new session/i }))
    await waitFor(() => expect(screen.getByText(/^connected/i)).toBeInTheDocument())

    await userEv.type(screen.getByRole('textbox'), 'busy')
    await userEv.keyboard('{Enter}')

    await waitFor(() => expect(screen.getByRole('button', { name: /^stop$/i })).toBeInTheDocument())

    await act(async () => {
      sseFeed.emit({
        type: 'ToolConfirmationRequest',
        tool_call_id: 'tc-stop',
        tool_name: 't',
        arguments: {},
      })
    })

    await waitFor(() => screen.getByTestId('tool-confirmation-modal'))
    await userEv.click(screen.getByRole('button', { name: /^stop$/i }))

    expect(
      fetchMock.mock.calls.some(
        ([u, cfg]) => String(u).includes('/cancel') && (cfg as RequestInit)?.method === 'POST',
      ),
    ).toBe(true)

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /tool confirmation/i })).not.toBeInTheDocument(),
    )
  })

  it('test_sse_consumer_end_clears_pending_modal', async () => {
    await renderConnected()
    await act(async () => {
      sseFeed.emit({
        type: 'ToolConfirmationRequest',
        tool_call_id: 'tc-fin',
        tool_name: 't',
        arguments: {},
      })
    })

    await waitFor(() => screen.getByTestId('tool-confirmation-modal'))
    await act(async () => {
      sseFeed.finish()
    })

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /tool confirmation/i })).not.toBeInTheDocument(),
    )
  })

  it('test_transcript_article_order_AB_around_png', async () => {
    await renderConnected()

    await act(async () => {
      sseFeed.emit({ type: 'AssistantDelta', request_id: 'a1', delta: 'A' })
    })
    await act(async () => {
      sseFeed.emit({ type: 'TurnComplete', request_id: 'a1', usage: {} } as GatewayJsonEvent)
    })
    await waitFor(() => expect(screen.getByText('A')).toBeInTheDocument())

    await act(async () => {
      sseFeed.emit({
        type: 'ImageBlock',
        mime_type: 'image/png',
        data: PNG_1PX,
        request_id: 'mid',
      })
    })
    await waitFor(() => expect(screen.getByRole('img', { name: /assistant image/i })).toBeInTheDocument())

    await act(async () => {
      sseFeed.emit({ type: 'AssistantDelta', request_id: 'a2', delta: 'B' })
    })
    await act(async () => {
      sseFeed.emit({ type: 'TurnComplete', request_id: 'a2', usage: {} } as GatewayJsonEvent)
    })
    await waitFor(() => expect(screen.getByText('B')).toBeInTheDocument())

    const log = screen.getByRole('log')
    const html = log.innerHTML
    const posA = html.indexOf('>A<')
    const posB = html.lastIndexOf('>B<')
    const imgPos = html.indexOf('data:image')
    expect(posA).not.toBe(-1)
    expect(posB).not.toBe(-1)
    expect(imgPos).not.toBe(-1)
    expect(posA).toBeLessThan(imgPos)
    expect(posB).toBeGreaterThan(imgPos)
  })

  it('test_elicitation_nested_schema_shows_message_submit_posts_null_user_data', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/elicitations/el_deep') && method === 'POST') {
        expect(JSON.parse(String(init?.body)).user_data).toBe(null)
        return new Response(JSON.stringify({ ok: true }), { status: 202 })
      }
      return Promise.resolve(new Response(`fallback ${method} ${url}`, { status: 500 }))
    })

    await renderConnected()

    await act(async () => {
      sseFeed.emit({
        type: 'ActionRequiredEvent',
        action_type: 'elicitation',
        id: 'el_deep',
        payload: {
          message: 'nested',
          requested_schema: {
            type: 'object',
            properties: {
              nested: {
                type: 'object',
                properties: { inner: { type: 'string' } },
              },
            },
            required: ['nested'],
          },
        },
      })
    })

    await waitFor(() => expect(screen.getByText(/Schema not supported in v1/i)).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /^Submit$/i }))

    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/elicitations/el_deep'))).toBe(true)
    await waitFor(() => expect(screen.queryByTestId('elicitation-form')).not.toBeInTheDocument())
  })

  it('test_unknown_fallback_copy_optional_callback', () => {
    const spy = vi.fn()
    render(<UnknownEventFallback title="oops" bodyText={`{"code": 1}`} onCopy={() => spy()} />)

    fireEvent.click(screen.getByRole('button', { name: /^copy$/i }))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('test_role_log_stable_after_odd_events', async () => {
    await renderConnected()

    await act(async () => {
      sseFeed.emit({ type: 'RedactedThinkingBlock', data: '', request_id: 'r' })
    })

    expect(screen.getByRole('log')).toBeTruthy()
  })

  it('test_attachment_descriptor_upgrades_user_chip', async () => {
    const postSpy = vi.spyOn(gw, 'postReply').mockResolvedValue(undefined as never)
    const userEv = userEvent.setup()
    await renderConnected()

    fetchMock.mockImplementation(async (input, init) => {
      const d = defaultFetchResponder(input, init)
      if (d) return Promise.resolve(d)
      return Promise.resolve(new Response('unexpected', { status: 500 }))
    })

    await userEv.click(screen.getByRole('button', { name: /^attach$/i }))
    const fileInput = document.querySelector('.composer-file-input') as HTMLInputElement
    const file = new File([new Uint8Array([1, 2, 3])], 'shot.png', { type: 'image/png' })
    await userEv.upload(fileInput, file)

    await waitFor(() => expect(screen.getByTestId('composer-pending-attachments')).toBeInTheDocument())

    await userEv.type(screen.getByRole('textbox'), 'see this')
    await userEv.click(screen.getByRole('button', { name: /^send$/i }))

    expect(postSpy).toHaveBeenCalled()
    const rid = postSpy.mock.calls[0]?.[1] as string

    await act(async () => {
      sseFeed.emit({
        type: 'AttachmentDescriptor',
        request_id: rid,
        attachment_id: 'att_test1',
        mime_type: 'image/png',
        filename: 'shot.png',
        description: 'User asked about the screenshot.',
      })
    })

    await waitFor(() => {
      const frozen = screen
        .getAllByTestId('attachment-chip')
        .find((c) => c.getAttribute('data-frozen') === 'true')
      expect(frozen).toBeTruthy()
      expect(frozen?.textContent).toContain('shot.png')
    })
  })
})

describe('session cost display (App usage strip)', () => {
  it('test_usage_strip_shows_dollar_zero_when_cost_zero', async () => {
    await renderConnected()

    await waitFor(() => {
      expect(screen.getByText('$0.00')).toBeInTheDocument()
      expect(screen.getByText('Cost')).toBeInTheDocument()
    })
  })

  it('test_usage_strip_shows_subcent_cost', async () => {
    vi.spyOn(gw, 'fetchSessionUsage').mockResolvedValue({ ...usageFixture, cost_usd: 0.0123 })
    await renderConnected()

    await waitFor(() => expect(screen.getByText('$0.0123')).toBeInTheDocument())
  })

  it('test_no_cost_item_when_not_connected', () => {
    render(<App />)

    expect(screen.queryByText(/\$\d/)).toBeNull()
    expect(screen.queryByText(/Cost/i)).toBeNull()
  })
})
