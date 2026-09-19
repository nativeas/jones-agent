import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    // `tests/acceptance/**` (Issue #24, docs/design/05-w6-interfaces.md §2):
    // W6's G/N-numbered acceptance wrappers, run by `pnpm test` like every
    // other suite so they're part of the normal PR gate, not a separate opt-in.
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', 'tests/acceptance/**/*.test.ts']
  }
})
