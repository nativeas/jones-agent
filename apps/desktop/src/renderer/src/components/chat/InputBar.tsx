import { useState, type KeyboardEvent } from 'react'

interface InputBarProps {
  running: boolean
  onSend: (text: string) => Promise<{ queued: boolean } | null>
  onStop: () => void
}

/** 输入框 + 停止按钮；运行中发送会入队（由 store 处理），这里只负责给出即时提示。 */
export function InputBar({ running, onSend, onStop }: InputBarProps): JSX.Element {
  const [text, setText] = useState('')
  const [hint, setHint] = useState<string | null>(null)

  const send = async (): Promise<void> => {
    const value = text
    if (!value.trim()) return
    setText('')
    const result = await onSend(value)
    if (result?.queued) {
      setHint('已加入队列，将在当前对话结束后发送')
      setTimeout(() => setHint(null), 3000)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <div className="input-bar">
      {hint && <div className="input-bar__hint">{hint}</div>}
      <div className="input-bar__row">
        <textarea
          className="input-bar__textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={running ? '运行中发送会进入队列…' : '给 Jones 发消息，Enter 发送，Shift+Enter 换行'}
          rows={2}
        />
        <div className="input-bar__actions">
          <button onClick={() => void send()} disabled={!text.trim()}>
            发送
          </button>
          {running && (
            <button className="input-bar__stop" onClick={onStop}>
              停止
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
