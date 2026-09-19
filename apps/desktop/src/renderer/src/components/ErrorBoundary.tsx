import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

/** jones-agent#34 / PRD G08 / 12.2 N16 ("不白屏"): a single uncaught render
 * error anywhere in the tree used to take down the entire app with no
 * recovery (this is what made #34's `message.content` bug a *white screen*
 * rather than one bad-looking bubble). This is the second, independent half
 * of that issue's fix — it does not replace aligning the types to the real
 * daemon payload (see domain/types.ts / MessageList.tsx), it's a backstop
 * for the next shape-drift nobody caught yet.
 *
 * Deliberately a class component: `componentDidCatch`/`getDerivedStateFromError`
 * are still the only way to catch a render error in React 18 — no hook
 * equivalent exists. */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Never silent (DEV.md 工程原则 #4) — this is the one place in the app
    // that must not itself throw trying to report the throw.
    console.error('renderer crashed', error, info.componentStack)
  }

  private reset = (): void => {
    this.setState({ error: null })
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="error-boundary">
        <div className="error-boundary__title">界面出错了</div>
        <div className="error-boundary__message">{error.message}</div>
        <button onClick={this.reset}>重试</button>
      </div>
    )
  }
}
