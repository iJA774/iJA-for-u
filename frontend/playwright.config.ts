import { defineConfig, devices } from '@playwright/test'

const e2ePort = Number(process.env.IJA_E2E_PORT ?? 8000)
const e2eBaseUrl = `http://127.0.0.1:${e2ePort}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: e2eBaseUrl,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `..\\.venv\\Scripts\\python.exe ../tests/e2e_server.py --port ${e2ePort}`,
    url: `${e2eBaseUrl}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
