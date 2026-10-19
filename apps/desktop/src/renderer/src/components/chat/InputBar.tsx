import { useState, type KeyboardEvent } from 'react'
import { Loader2, Send, Square } from 'lucide-react'
import { cn } from '../../lib/utils'
import { BlurFade } from '../ui/blur-fade'

interface InputBarProps {
  running: boolean
  onSend: (text: string) => Promise<{ queued: boolean } | null>
  onStop: () => void
}

/** 输入框 + 停止按钮；运行中发送会入队（由 store 处理），这里只负责给出即时提示。
 *
 * `input-bar__textarea` / `input-bar__actions` 这两个 class 名保留（不只是
 * Tailwind 装饰）——`scripts/e2e.mjs` 和 `tests/perf/measure-startup.mjs`
 * 用它们做选择器（前者还依赖发送按钮是 `.input-bar__actions button` 里的第一
 * 个）；`!` 前缀的工具类用来盖掉 styles.css 里这两个类残留的旧边框/圆角/resize
 * 声明（同一层叠层级，后定义的旧规则本会赢）。 */
export function InputBar({ running, onSend, onStop }: InputBarProps): JSX.Element {
  const [text, setText] = useState('')
  const [hint, setHint] = useState<string | null>(null)
  const [sending, setSending] = useState(false)

  const send = async (): Promise<void> => {
    const value = text
    if (!value.trim() || sending) return
    setText('')
    setSending(true)
    try {
      const result = await onSend(value)
      if (result?.queued) {
        setHint('已加入队列，将在当前对话结束后发送')
        setTimeout(() => setHint(null), 3000)
      }
    } finally {
      setSending(false)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <div className="border-t border-neutral-200 px-3 py-2 dark:border-neutral-800">
      {hint && (
        <BlurFade
          duration={0.2}
          offset={4}
          direction="up"
          className="mb-1.5 text-xs text-neutral-500 dark:text-neutral-400"
        >
          {hint}
        </BlurFade>
      )}
      <div className="flex items-end gap-2 rounded-2xl border border-neutral-200 bg-white px-2 py-1.5 transition-colors focus-within:border-blue-500 focus-within:ring-1 focus-within:ring-blue-500 dark:border-neutral-700 dark:bg-neutral-900">
        <textarea
          className="input-bar__textarea !resize-none !border-0 !bg-transparent !p-1.5 max-h-40 flex-1 text-sm text-neutral-900 outline-none placeholder:text-neutral-400 dark:text-neutral-100 dark:placeholder:text-neutral-500"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={running ? '运行中发送会进入队列…' : '给 Jones 发消息，Enter 发送，Shift+Enter 换行'}
          rows={2}
        />
        <div className="input-bar__actions flex flex-col items-center gap-1.5 pb-0.5">
          <button
            type="button"
            onClick={() => void send()}
            disabled={!text.trim() || sending}
            aria-label="发送"
            className={cn(
              'flex h-8 w-8 items-center justify-center rounded-full bg-blue-600 text-white transition-colors',
              'disabled:bg-neutral-200 disabled:text-neutral-400 dark:disabled:bg-neutral-800 dark:disabled:text-neutral-600'
            )}
          >
            {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </button>
          {running && (
            <button
              type="button"
              onClick={onStop}
              aria-label="停止"
              className="flex h-8 w-8 items-center justify-center rounded-full border border-red-200 text-red-500 transition-colors hover:bg-red-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950"
            >
              <Square className="h-3.5 w-3.5" fill="currentColor" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
