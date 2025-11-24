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
        
        if not self.current_cookies:
            print("⚠️ Cloud Harvester: No cookies available. Waiting for update via /admin...")
        
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
                            self.current_cookies = None
                            await asyncio.sleep(10)
                            continue

                    self.page = await context.new_page()
                    
                    # 1. 拦截请求 (用于提取数据)
                    await self.page.route("**/*", self.handle_route)
                    # 2. 监听响应 (用于检测 Cookie 失效/401错误)
                    self.page.on("response", self.handle_response)
                    
                    print(f"☁️ Cloud Harvester: Navigating to {VERTEX_URL}...")
                    try:
                        await self.page.goto(VERTEX_URL, timeout=60000, wait_until="domcontentloaded")
                    except Exception as e:
                        print(f"❌ Cloud Harvester: Navigation failed: {e}")
                    
                    self.restart_requested = False
                    self.refresh_needed = False

                    # --- Inner Loop (Session) ---
                    while self.is_running and not self.restart_requested:
                        
                        # Case A: 检测到需要刷新 (由 handle_response 触发)
                        if self.refresh_needed:
                            print("♻️ Cloud Harvester: Token expired (401/403 detected). Refreshing page...")
                            try:
                                await self.page.reload(wait_until="domcontentloaded")
                                print("✅ Page reloaded. Re-triggering harvest immediately...")
                                self.refresh_needed = False
                                await asyncio.sleep(5) # 等待页面稳定
                                await self.perform_harvest() # 重新获取热重载
                            except Exception as e:
                                print(f"⚠️ Refresh failed: {e}")
                            continue

                        # Case B: 页面跳转到了登录页 (Hard Expiry)
                        if "accounts.google.com" in self.page.url or "Sign in" in await self.page.title():
                            current_time = time.time()
                            # 如果距离上次重试超过60秒，尝试救活一次
                            if current_time - self.last_login_retry_time > 60:
                                print("⚠️ Cloud Harvester: Redirected to Login. Trying to navigate back to Vertex (Retry)...")
                                self.last_login_retry_time = current_time
                                try:
                                    await self.page.goto(VERTEX_URL, wait_until="domcontentloaded")
                                    await asyncio.sleep(5)
                                    # 如果跳转回来还是登录页，下一次循环会被下面的 else 捕获
                                    continue 
                                except Exception:
                                    pass
                            else:
                                print("❌ Cloud Harvester: Cookies Expired (Login Page loop detected).")
                                print("   👉 Please export fresh cookies.")
                                break # 退出内层循环，等待新 Cookie 或重启

                        # Case C: 正常定时采集
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

    # --- 监听响应，检测失效 Token ---
    async def handle_response(self, response):
        try:
            # 检测 batchGraphql 接口是否返回 401 (未授权) 或 403 (禁止)
            if "batchGraphql" in response.url:
                if response.status in [401, 403]:
                    print(f"⚠️ Cloud Harvester: API returned {response.status}. Marking for refresh.")
                    self.refresh_needed = True
        except:
            pass

    async def handle_route(self, route):
        request = route.request
        if "batchGraphql" in request.url and request.method == "POST":
            try:
                post_data = request.post_data
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
                    # 成功采集一次，重置登录重试计时，说明当前 Cookie 还是有效的
                    self.last_login_retry_time = 0 
            except Exception as e:
                print(f"⚠️ Cloud Harvester: Error analyzing request: {e}")
        await route.continue_()

    async def perform_harvest(self):
        print("🤖 Cloud Harvester: Attempting to trigger request...")
        if not self.page: return

        try:
            # 1. 处理条款弹窗 (保持原有逻辑)
            terms_checkbox = 'mat-checkbox:has-text("Accept terms of use"), mat-checkbox:has-text("接受使用条款")'
            agree_btn = 'button:has-text("Agree"), button:has-text("同意")'
            dialog_content = 'div.mat-mdc-dialog-content'

            if await self.page.is_visible(dialog_content):
                print("🧹 Cloud Harvester: Terms Dialog detected.")
                try:
                    await self.page.evaluate(f"document.querySelector('{dialog_content}').scrollTop = document.querySelector('{dialog_content}').scrollHeight")
                except: pass
                
                # 勾选
                await self.page.evaluate(f"""
                    const cb = document.querySelector('mat-checkbox:has-text("Accept terms of use") input') || document.querySelector('mat-checkbox:has-text("接受使用条款") input');
                    if(cb) cb.click();
                """)
                await asyncio.sleep(1) 

                # 点击同意
                await self.page.evaluate(f"""
                    document.querySelectorAll('button:has-text("Agree"), button:has-text("同意")').forEach(b => {{
                        b.disabled = false;
                        b.click();
                    }})
                """)
                try:
                    await self.page.wait_for_selector(dialog_content, state='hidden', timeout=3000)
                except: pass

            # 处理其他弹窗
            popup_selectors = ['button[aria-label="Close"]', 'button:has-text("Got it")', 'button:has-text("OK")']
            for selector in popup_selectors:
                try:
                    if await self.page.is_visible(selector):
                        await self.page.click(selector)
                except: pass

            # 2. 发送文本 "Hello"
            editor_selector = 'div[contenteditable="true"]'
            
            print("⏳ Cloud Harvester: Waiting for editor...")
            # 如果这里超时，可能页面也是假死状态，设为需要刷新
            try:
                await self.page.wait_for_selector(editor_selector, state="visible", timeout=5000)
            except:
                print("⚠️ Editor not found (timeout). Page might be stuck.")
                # 可以在这里选择性地设置 self.refresh_needed = True
                return 

            await self.page.click(editor_selector, force=True)
            await self.page.evaluate(f"document.querySelector('{editor_selector}').innerText = ''")
            await self.page.fill(editor_selector, "Hello")
            await asyncio.sleep(0.5)
            
            print("🚀 Cloud Harvester: Sending 'Hello'...")
            await self.page.press(editor_selector, "Enter")
            
            # 给一点时间让 handle_route 捕获
            await asyncio.sleep(3)
            
        except Exception as e:
            print(f"❌ Cloud Harvester: Interaction failed: {e}")
