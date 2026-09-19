import { describe, expect, it } from 'vitest'
import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { TerminationCard } from '../chat/TerminationCard'
import type { ErrorCard, Model, Provider, TerminationCard as TerminationCardData } from '../../domain/types'

/** Issue #22 (FR14, 04-w5-interfaces.md §4) — real DOM mounts, same convention
 * `DaemonStatusCard.test.tsx` established: a rendering bug (white screen, N16)
 * can only be demonstrated by actually mounting the component, not by unit-
 * testing a formatting helper. Covers G08's "UI 无白屏" requirement and the
 * three card actions being real, wired buttons. */

function mount(el: JSX.Element): { container: HTMLDivElement; root: Root; unmount: () => void } {
  const container = document.createElement('div')
  document.body.appendChild(container)
  let root: Root
  act(() => {
    root = createRoot(container)
    root.render(el)
  })
  return {
    container,
    root: root!,
    unmount: () => {
      act(() => root.unmount())
      container.remove()
    }
  }
}

function errorCard(overrides: Partial<ErrorCard> = {}): TerminationCardData {
  return {
    run_id: 'run_1',
    turn_id: 'turn_1',
    kind: 'error',
    reason: 'Connection refused',
    card: {
      kind: 'network',
      title: '网络错误',
      message: 'Connection refused',
      step_seq: 2,
      raw_excerpt: 'Connection refused while calling api.anthropic.com',
      actions: ['retry', 'switch_model', 'abandon'],
      retryable: true,
      ...overrides
    }
  }
}

const providers: Provider[] = [{ provider: 'openai', has_key: true, key_hint: 'abcd', default_model: null }]
const models: Model[] = [{ provider: 'openai', id: 'gpt-5', label: 'GPT-5' }]

describe('TerminationCard', () => {
  it('renders a user-stop card with no action buttons (nothing to retry)', () => {
    const card: TerminationCardData = {
      run_id: 'run_1',
      turn_id: 'turn_1',
      kind: 'user',
      reason: '用户手动停止',
      card: {
        kind: 'user',
        title: '已停止',
        message: '用户手动停止，正在执行的动作已收尾。',
        step_seq: null,
        raw_excerpt: '',
        actions: [],
        retryable: false
      }
    }
    const { container, unmount } = mount(<TerminationCard card={card} providers={providers} models={models} />)
    try {
      expect(container.textContent).toContain('已停止')
      expect(container.querySelectorAll('.termination-card__actions button')).toHaveLength(0)
    } finally {
      unmount()
    }
  })

  it('renders a network error card with all three actions and a collapsible raw excerpt', () => {
    const card = errorCard()
    const clicks = { retry: 0, abandon: 0 }
    const { container, unmount } = mount(
      <TerminationCard
        card={card}
        providers={providers}
        models={models}
        onRetry={() => (clicks.retry += 1)}
        onAbandon={() => (clicks.abandon += 1)}
      />
    )
    try {
      expect(container.textContent).toContain('网络错误')
      expect(container.textContent).toContain('发生在第 2 步')
      expect(container.querySelector('summary')?.textContent).toBe('查看原始错误')
      expect(container.textContent).toContain('Connection refused while calling api.anthropic.com')

      const buttons = Array.from(container.querySelectorAll('.termination-card__actions button'))
      expect(buttons.map((b) => b.textContent)).toEqual(['重试', '换模型', '放弃'])

      act(() => {
        ;(buttons[0] as HTMLButtonElement).click()
      })
      expect(clicks.retry).toBe(1)

      act(() => {
        ;(buttons[2] as HTMLButtonElement).click()
      })
      expect(clicks.abandon).toBe(1)
    } finally {
      unmount()
    }
  })

  it('provider_auth cards offer switch_model/abandon but never a bare retry', () => {
    const card = errorCard({
      kind: 'provider_auth',
      title: '模型 Key 无效',
      actions: ['switch_model', 'abandon'],
      retryable: false
    })
    const { container, unmount } = mount(<TerminationCard card={card} providers={providers} models={models} />)
    try {
      const buttons = Array.from(container.querySelectorAll('.termination-card__actions button'))
      expect(buttons.map((b) => b.textContent)).toEqual(['换模型', '放弃'])
    } finally {
      unmount()
    }
  })

  it('"换模型" opens an inline picker; confirming calls onSwitchModel with the chosen provider/model', () => {
    const card = errorCard()
    let received: { provider: string; model: string } | null = null
    const { container, unmount } = mount(
      <TerminationCard
        card={card}
        providers={providers}
        models={models}
        onSwitchModel={(override) => {
          received = override
        }}
      />
    )
    try {
      const switchBtn = Array.from(container.querySelectorAll('.termination-card__actions button')).find(
        (b) => b.textContent === '换模型'
      ) as HTMLButtonElement
      act(() => switchBtn.click())

      const confirmBtn = Array.from(container.querySelectorAll('.termination-card__model-picker button')).find(
        (b) => b.textContent === '确认重试'
      ) as HTMLButtonElement
      expect(confirmBtn).toBeDefined()
      act(() => confirmBtn.click())

      expect(received).toEqual({ provider: 'openai', model: 'gpt-5' })
    } finally {
      unmount()
    }
  })

  it('"换模型" with no configured providers shows a guidance message instead of a broken picker', () => {
    const card = errorCard()
    const { container, unmount } = mount(<TerminationCard card={card} providers={[]} models={[]} />)
    try {
      const switchBtn = Array.from(container.querySelectorAll('.termination-card__actions button')).find(
        (b) => b.textContent === '换模型'
      ) as HTMLButtonElement
      act(() => switchBtn.click())

      expect(container.textContent).toContain('还没有配置好 Key 的模型厂商')
    } finally {
      unmount()
    }
  })

  it('N16: never white-screens on a partial/older card shape missing optional fields', () => {
    const card = {
      run_id: 'run_1',
      turn_id: 'turn_1',
      kind: 'error',
      reason: 'x',
      card: { kind: 'internal', title: '内部错误', message: 'x', actions: ['retry', 'abandon'] }
      // step_seq / raw_excerpt / retryable deliberately absent
    } as unknown as TerminationCardData
    expect(() => {
      const { unmount } = mount(<TerminationCard card={card} providers={providers} models={models} />)
      unmount()
    }).not.toThrow()
  })
})
