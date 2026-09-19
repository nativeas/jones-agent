/**
 * The complete RPC v0 method surface (front end → daemon), mirroring
 * docs/design/00-foundation.md §4.1's table plus two documented additions:
 *
 *   - `session.subscribe`/`session.unsubscribe` — declared in §4.2's own header
 *     ("按 session 订阅：session.subscribe {id} / session.unsubscribe"), not as a
 *     row in the §4.1 table itself, but a real method every RPC connection must
 *     be able to call (chatStore.ts's bindSession() already calls both).
 *   - `run.list`/`run.payload` — added by docs/design/02-w3-interfaces.md §2
 *     (issue #12, FR06 回放), on top of §4.1's original `run.get`/`run.steps`.
 *     §4.1 never had a way to discover a historical Run id at all besides the
 *     live `turn.started` notification — `run.list` is what the 回放 view's
 *     "选一个 Run" list actually calls.
 *
 * `apps/desktop/src/main/index.ts`'s `ALLOWED_RPC_METHODS` is this exact set —
 * `src/shared/__tests__/rpcMethods.test.ts` parses 00-foundation.md §4.1's table
 * directly and asserts this file didn't drift from it (minus the two documented
 * additions above, which that test also checks for explicitly).
 *
 * Method names not yet implemented server-side (`cron.*`, `capability.list` —
 * no issue has landed them yet) are still listed and allowed: this is a security
 * allowlist against the untrusted renderer (02-w3-interfaces.md §0), not a
 * feature-readiness list — calling one before it's implemented just gets a
 * normal `method_not_found` from the daemon, the same as any other unregistered
 * method, and every future issue implementing one of these does not need to
 * touch main/index.ts again.
 */
export const RPC_V0_METHODS = [
  'daemon.ping',
  'daemon.status',
  'project.list',
  'project.create',
  'project.delete',
  'agent.list',
  'agent.get',
  'agent.upsert',
  'agent.delete',
  'session.list',
  'session.create',
  'session.get',
  'session.set_mode',
  'session.send',
  'session.queue',
  'session.queue_remove',
  'session.queue_reorder',
  'session.stop',
  'session.subscribe',
  'session.unsubscribe',
  'turn.messages',
  'run.list',
  'run.get',
  'run.steps',
  'run.payload',
  'permission.pending',
  'permission.decide',
  'provider.list',
  'provider.set_key',
  'provider.delete_key',
  'model.list',
  'capability.list',
  'cron.list',
  'cron.upsert',
  'cron.delete',
  'cron.run_now',
  'settings.get',
  'settings.set'
] as const

export type RpcV0Method = (typeof RPC_V0_METHODS)[number]
