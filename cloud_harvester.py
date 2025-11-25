import asyncio
import json
import os
import time
from playwright.async_api import async_playwright, Page

# --- Configuration ---
VERTEX_URL = "https://console.cloud.google.com/vertex-ai/studio/multimodal?mode=prompt&model=gemini-2.5-flash-lite-preview-09-2025"
COOKIES_ENV_VAR = "GOOGLE_COOKIES"

class CloudHarvester:
    def __init__(self, cred_manager):
        self.cred_manager = cred_manager
        self.browser = None
        self.page = None
        self.is_running = False
        self.last_harvest_time = 0
        self.current_cookies = os.environ.get(COOKIES_ENV_VAR)
        self.restart_requested = False
        
        # New: 状态标记
        self.refresh_needed = False
        self.last_login_retry_time = 0

    async def update_cookies(self, new_cookies_json):
        """Updates cookies and triggers a browser restart."""
        print("🍪 Cloud Harvester: Received new cookies. Scheduling restart...")
        self.current_cookies = new_cookies_json
        self.restart_requested = True

    async def start(self):
        """Starts the browser and the harvesting loop."""
        if self.is_running:
            return
        
        print("☁️ Cloud Harvester: Starting...")
        self.is_running = True
        
        while self.is_running:
            try:
                async with async_playwright() as p:
                    self.browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
                    context = await self.browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                    
                    if self.current_cookies:
                        try:
                            cookies = json.loads(self.current_cookies)
                            await context.add_cookies(cookies)
                            print(f"🍪 Cloud Harvester: Loaded {len(cookies)} cookies.")
                        except json.JSONDecodeError:
                            print("❌ Cloud Harvester: Invalid JSON in cookies.")
                            self.current_cookies = None # Reset invalid cookies
                            await asyncio.sleep(10)
                            continue

                    self.page = await context.new_page()
                    
                    # 1. 拦截请求
                    await self.page.route("**/*", self.handle_route)
                    # 2. 监听响应 (检测 401/403)
                    self.page.on("response", self.handle_response)
                    
                    print(f"☁️ Cloud Harvester: Navigating to {VERTEX_URL}...")
                    try:
                        await self.page.goto(VERTEX_URL, timeout=60000, wait_until="domcontentloaded")
                    except Exception as e:
                        print(f"❌ Cloud Harvester: Navigation failed: {e}")
                    
                    self.restart_requested = False
                    self.refresh_needed = False
                    
                    # Inner Loop
                    while self.is_running and not self.restart_requested:
                        
                        # A. 自动刷新检测 (Recaptcha token invalid / 401 / 403)
                        if self.refresh_needed:
                            print("♻️ Cloud Harvester: Token invalid or expired. Refreshing page...")
                            try:
                                await self.page.reload(wait_until="domcontentloaded")
                                self.refresh_needed = False
                                await asyncio.sleep(5)
                                await self.perform_harvest() # 立即尝试交互
                            except Exception as e:
                                print(f"⚠️ Refresh failed: {e}")
                            continue

                        # B. 登录页跳转检测
                        if "accounts.google.com" in self.page.url or "Sign in" in await self.page.title():
                            current_time = time.time()
                            if current_time - self.last_login_retry_time > 60:
                                print("⚠️ Cloud Harvester: Redirected to Login. Trying to navigate back (Retry)...")
                                self.last_login_retry_time = current_time
                                try:
                                    await self.page.goto(VERTEX_URL, wait_until="domcontentloaded")
                                    await asyncio.sleep(5)
                                    continue 
                                except: pass
                            else:
                                print("❌ Cloud Harvester: Cookies Expired (Login Page detected).")
                                break 

                        # C. 定时采集
                        if time.time() - self.last_harvest_time > 2700 or not self.cred_manager.latest_harvest:
                            await self.perform_harvest()
                        
                        await asyncio.sleep(5)
                    
                    await self.browser.close()
                    if self.restart_requested:
                        print("♻️ Cloud Harvester: Restarting with new cookies...")

            except Exception as e:
                print(f"❌ Cloud Harvester Error: {e}")
                await asyncio.sleep(10)
        
        print("☁️ Cloud Harvester: Stopped.")

    async def handle_response(self, response):
        try:
            # 检测接口错误，如果 Recaptcha 失效通常也会导致接口报错
            if "batchGraphql" in response.url:
                if response.status in [400, 401, 403]:
                    # 400 经常对应 Bad Request (Recaptcha Token Invalid)
                    # 401/403 对应 Auth 失效
                    print(f"⚠️ Cloud Harvester: API returned {response.status}. Marking for refresh.")
                    self.refresh_needed = True
        except:
            pass

    async def handle_route(self, route):
        request = route.request
        if "batchGraphql" in request.url and request.method == "POST":
            try:
                post_data = request.post_data
                # 只要是生成内容的请求，都尝试抓取
                if post_data and ("StreamGenerateContent" in post_data or "generateContent" in post_data):
                    print("🎯 Cloud Harvester: Captured Target Request!")
                    harvest_data = {
                        "url": request.url,
                        "method": request.method,
                        "headers": request.headers,
                        "body": post_data
                    }
                    self.cred_manager.update(harvest_data)
                    self.last_harvest_time = time.time()
                    self.last_login_retry_time = 0 
                    
                    # Signal that the refresh sequence is complete
                    print("☁️ Cloud Harvester: Signaling refresh complete.")
                    self.cred_manager.refresh_complete_event.set()
                    
            except Exception as e:
                print(f"⚠️ Cloud Harvester: Error analyzing request: {e}")
        await route.continue_()

    async def perform_harvest(self):
        print("🤖 Cloud Harvester: Attempting to trigger request...")
        if not self.page: return

        try:
            # ============================================================
            # 1. 处理条款弹窗 (修复了 SyntaxError)
            # 使用原生 JS 遍历元素，替代不兼容的 Selector
            # ============================================================
            dialog_content = 'div.mat-mdc-dialog-content'
            if await self.page.is_visible(dialog_content):
                print("🧹 Cloud Harvester: Terms Dialog detected. Handling via JS...")
                
                # 1.1 滚动 (防止点击被遮挡)
                await self.page.evaluate(f"""
                    const d = document.querySelector('{dialog_content}');
                    if(d) d.scrollTop = d.scrollHeight;
                """)
                await asyncio.sleep(0.5)

                # 1.2 查找并勾选 (原生 JS 查找包含文本的元素)
                await self.page.evaluate("""
                    // 查找包含 Accept 或 接受 的 checkbox
                    const checkboxes = Array.from(document.querySelectorAll('mat-checkbox'));
                    const targetCb = checkboxes.find(cb => 
                        cb.innerText.includes("Accept terms of use") || 
                        cb.innerText.includes("接受使用条款")
                    );
                    
                    if (targetCb) {
                        // 尝试点击 input 元素，如果没有则点击 host
                        const input = targetCb.querySelector('input');
                        if (input) input.click();
                        else targetCb.click();
                    }
                """)
                
                print("   - Checkbox ticked (if found). Waiting for button...")
                await asyncio.sleep(1.5)

                # 1.3 查找并点击同意按钮 (原生 JS)
                await self.page.evaluate("""
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const agreeBtn = buttons.find(b => 
                        (b.innerText.includes("Agree") || b.innerText.includes("同意")) && 
                        !b.innerText.includes("Disagree") // 防止误触
                    );
                    
                    if (agreeBtn) {
                        agreeBtn.disabled = false; // 移除禁用状态
                        agreeBtn.click();
                    }
                """)
                
                # 等待弹窗消失
                try:
                    await self.page.wait_for_selector(dialog_content, state='hidden', timeout=3000)
                    print("   - Dialog closed.")
                except: pass

            # 处理普通提示弹窗 (Got it / Close)
            # 这里使用 Playwright 选择器是安全的，因为这些是标准 CSS
            popup_selectors = ['button[aria-label="Close"]', 'button[aria-label="Dismiss"]', 'button:has-text("Got it")', 'button:has-text("OK")']
            for selector in popup_selectors:
                try:
                    if await self.page.is_visible(selector):
                        await self.page.click(selector)
                except: pass

            # ============================================================
            # 2. 发送文本 "Hello"
            # ============================================================
            editor_selector = 'div[contenteditable="true"]'
            
            print("⏳ Cloud Harvester: Waiting for editor...")
            try:
                # 等待编辑器出现
                await self.page.wait_for_selector(editor_selector, state="visible", timeout=8000)
                
                # 确保焦点
                await self.page.click(editor_selector, force=True)
                
                # 清空并输入
                await self.page.evaluate(f"document.querySelector('{editor_selector}').innerText = ''")
                await self.page.fill(editor_selector, "Hello")
                await asyncio.sleep(0.5)
                
                print("🚀 Cloud Harvester: Sending 'Hello'...")
                await self.page.press(editor_selector, "Enter")
                
                # 等待网络请求被 handle_route 捕获
                await asyncio.sleep(5)
                
            except Exception as e:
                print(f"⚠️ Editor interaction skipped: {e}")
                # 如果找不到编辑器，可能是页面还在加载，或者需要刷新
                # 可以在这里不做处理，依靠 handle_response 来决定是否刷新

        except Exception as e:
            print(f"❌ Cloud Harvester: Interaction failed: {e}")
