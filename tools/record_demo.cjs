/* 演示视频录制（本地工具，不进 CI；产物 docs/assets/demo-90s.webm）。
 *
 * 前置：
 *   1. 构建前端产物：cd web/frontend && npm run build
 *   2. 起演示后端（零 LLM）：
 *      DR_DEMO=1 DR_DEMO_STEP_SECONDS=0.4 DR_SUBMIT_RATE_PER_MINUTE=0 \
 *      DR_LOGIN_RATE_PER_MINUTE=0 DR_LOGIN_ACCOUNT_RATE_PER_MINUTE=0 \
 *      OTEL_SDK_DISABLED=true python -m uvicorn web.backend.main:app --host 127.0.0.1 --port 8011
 *   3. 录制（需要本机 Chrome；Playwright 浏览器在 e2e 流程中已安装）：
 *      NODE_PATH=web/frontend/node_modules node tools/record_demo.cjs
 *
 * 说明：录的是**构建产物 + DR_DEMO 假图**，事件序列/阶段/报告/证据与真实链路一致，
 * 不调用 LLM、不产生费用；UI 更新后需重新录制（基线截图见 e2e/visual-baseline.spec.ts）。
 */
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("@playwright/test");

const BASE = process.env.DEMO_BASE_URL || "http://127.0.0.1:8011";
const OUT_DIR = process.env.DEMO_VIDEO_DIR || path.join("docs", "assets");
const OUT_NAME = "demo-90s.webm";

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    reducedMotion: "reduce",
    recordVideo: { dir: ".tmp/demo-video", size: { width: 1280, height: 720 } },
  });
  const page = await context.newPage();
  const video = page.video();
  const wait = (ms) => page.waitForTimeout(ms);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await wait(14000); // 落地页：标题 + 能力说明
  await page.evaluate(() => window.scrollTo(0, 420));
  await wait(9000); // 能力/边界区
  await page.evaluate(() => window.scrollTo(0, 0));
  await wait(3000);

  const topicInput = page.locator("#topic");
  await topicInput.scrollIntoViewIfNeeded();
  await wait(3000);
  await topicInput.fill("RAG 的基本原理是什么？");
  await wait(5000); // 展示输入
  await page.locator('button[type="submit"]').first().click();
  await wait(6000); // 运行中：阶段/progress

  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    const text = await page
      .locator('[data-testid="status-badge"]')
      .first()
      .textContent()
      .catch(() => "");
    if (/研究完成|已取消|已到时限停止|运行失败/.test(text || "")) break;
    await wait(500);
  }
  await wait(5000);
  await page.locator("#stage-report").scrollIntoViewIfNeeded().catch(() => {});
  await wait(9000); // 报告正文
  await page.evaluate(() => window.scrollBy(0, 600));
  await wait(8000); // 引用/证据
  await page.evaluate(() => window.scrollBy(0, 600));
  await wait(6000);
  await page.locator("#stage-verify").scrollIntoViewIfNeeded().catch(() => {});
  await wait(8000); // 证据栏
  await page.getByTestId("history-toggle").click().catch(() => {});
  await wait(8000); // 历史面板
  await page.getByTestId("kb-toggle").click().catch(() => {});
  await wait(9000); // 知识库面板
  await page.evaluate(() => window.scrollTo(0, 0));
  await wait(7000); // 收尾

  await context.close();
  const tmpPath = await video.path();
  await browser.close();
  const target = path.join(OUT_DIR, OUT_NAME);
  fs.copyFileSync(tmpPath, target);
  console.log("saved:", path.resolve(target));
})();
