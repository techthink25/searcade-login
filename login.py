import os
import platform
import time
import random
import re
from typing import List, Dict, Optional, Tuple

import requests
from seleniumbase import SB
from pyvirtualdisplay import Display

"""
批量登录 https://searcade.com （通过 userveria SSO + OAuth）

流程：
  1) 打开 https://searcade.com/en/admin/servers/<SERVER_ID>
     - 未登录 -> searcade 邮箱页：输入 email -> Continue with email
  2) 跳转到 https://userveria.com/authorize/?client_id=...
     - 输入密码 -> 提交
     - 如出现 OAuth 同意页（Authorize / Allow），点击同意按钮
  3) 跳回 searcade 服务器控制台页，停留 4-6 秒
  4) 返回 searcade 首页 https://searcade.com/，停留 3-5 秒
  5) 退出（点击 logout 或访问 /logout 路径）

环境变量：
  - ACCOUNTS_BATCH 多行账号，逗号分隔。支持 2/3/4/5 列：
      email,password
      email,password,server_id
      email,password,tg_bot_token,tg_chat_id
      email,password,server_id,tg_bot_token,tg_chat_id
  - SEARCADE_SERVER_ID 可选，默认 6927
"""

HOME_URL = "https://searcade.com/"
DEFAULT_SERVER_ID = os.getenv("SEARCADE_SERVER_ID", "6927").strip() or "6927"
SERVER_URL_TPL = "https://searcade.com/en/admin/servers/{server_id}"

SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# ---------- 选择器候选 ----------
EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[id="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="mail" i]',
]
CONTINUE_BTN_SELECTORS = [
    'button:contains("Continue with email")',
    'button:contains("Continue")',
    'button[type="submit"]',
    'input[type="submit"]',
]

PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[id="password"]',
    'input[autocomplete="current-password"]',
]
PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]:not([name="allow"]):not([name="deny"])',
    'button:contains("Log in")',
    'button:contains("Login")',
    'button:contains("Sign in")',
    'button:contains("Continue")',
    'button[type="submit"]',
    'input[type="submit"]',
]

# OAuth 同意 / 授权按钮（紧跟密码提交之后可能出现）
AUTHORIZE_BTN_SELECTORS = [
    'button[name="allow"]',
    'button[value="allow"]',
    'input[name="allow"]',
    'button:contains("Authorize")',
    'button:contains("Allow")',
    'button:contains("Approve")',
    'button:contains("Accept")',
    'button:contains("Continue")',
    'button:contains("Yes")',
    'a:contains("Authorize")',
    'a:contains("Allow")',
    'a:contains("Continue")',
    'input[type="submit"][value*="Allow" i]',
    'input[type="submit"][value*="Authorize" i]',
    'input[type="submit"][value*="Continue" i]',
    'form button[type="submit"]',
    'button[type="submit"]',
]

LOGOUT_LINK_SELECTORS = [
    'a[href$="/logout"]',
    'a[href*="/logout"]',
    'button:contains("Log out")',
    'button:contains("Logout")',
    'button:contains("Sign out")',
    'a:contains("Log out")',
    'a:contains("Logout")',
    'a:contains("Sign out")',
]
LOGOUT_URL_CANDIDATES = [
    "https://searcade.com/en/logout",
    "https://searcade.com/logout",
    "https://searcade.com/accounts/logout/",
]


def mask_email_keep_domain(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return "***"
    name, domain = e.split("@", 1)
    if len(name) <= 1:
        name_mask = name or "*"
    elif len(name) == 2:
        name_mask = name[0] + name[1]
    else:
        name_mask = name[0] + ("*" * (len(name) - 2)) + name[-1]
    return f"{name_mask}@{domain}"


def setup_xvfb():
    if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
        display = Display(visible=False, size=(1920, 1080))
        display.start()
        os.environ["DISPLAY"] = display.new_display_var
        print("🖥️ Xvfb 已启动")
        return display
    return None


def screenshot(sb, name: str):
    path = f"{SCREENSHOT_DIR}/{name}"
    try:
        sb.save_screenshot(path)
        print(f"📸 {path}")
    except Exception as e:
        print(f"⚠️ 截图失败 {path}: {e}")


def dump_html(sb, name: str):
    """落盘当前页面 HTML（去除密码值），便于调试。"""
    try:
        html = sb.get_page_source() or ""
        # 安全：把 password 字段的 value 干掉
        html = re.sub(
            r'(<input[^>]*type=["\']?password["\']?[^>]*?)\bvalue=("[^"]*"|\'[^\']*\')',
            r"\1",
            html,
            flags=re.IGNORECASE,
        )
        path = f"{SCREENSHOT_DIR}/{name}"
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
        print(f"📄 {path}")
    except Exception as e:
        print(f"⚠️ HTML 落盘失败 {name}: {e}")


def tg_send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None):
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"⚠️ TG 发送失败：{e}")


def build_accounts_from_env() -> List[Dict[str, str]]:
    batch = (os.getenv("ACCOUNTS_BATCH") or "").strip()
    if not batch:
        raise RuntimeError("❌ 缺少环境变量：请设置 ACCOUNTS_BATCH（即使只有一个账号也用它）")

    accounts: List[Dict[str, str]] = []
    for idx, raw in enumerate(batch.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) not in (2, 3, 4, 5):
            raise RuntimeError(
                f"❌ ACCOUNTS_BATCH 第 {idx} 行格式不对：{raw!r}"
            )
        email, password = parts[0], parts[1]
        server_id = DEFAULT_SERVER_ID
        tg_token = ""
        tg_chat = ""
        if len(parts) == 3:
            server_id = parts[2] or DEFAULT_SERVER_ID
        elif len(parts) == 4:
            tg_token = parts[2]
            tg_chat = parts[3]
        elif len(parts) == 5:
            server_id = parts[2] or DEFAULT_SERVER_ID
            tg_token = parts[3]
            tg_chat = parts[4]
        if not email or not password:
            raise RuntimeError(f"❌ ACCOUNTS_BATCH 第 {idx} 行存在空字段：{raw!r}")
        accounts.append(
            {
                "email": email,
                "password": password,
                "server_id": server_id,
                "tg_token": tg_token,
                "tg_chat": tg_chat,
            }
        )
    if not accounts:
        raise RuntimeError("❌ ACCOUNTS_BATCH 里没有有效账号行")
    return accounts


# ---------- 通用辅助 ----------
def _first_visible(sb, selectors: List[str], timeout_each: float = 1.5) -> Optional[str]:
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    for sel in selectors:
        try:
            sb.wait_for_element_visible(sel, timeout=timeout_each)
            return sel
        except Exception:
            continue
    return None


def _has_cf_clearance(sb: SB) -> bool:
    try:
        cookies = sb.get_cookies()
        cf_clearance = next((c["value"] for c in cookies if c.get("name") == "cf_clearance"), None)
        print("🧩 cf_clearance:", "OK" if cf_clearance else "NONE")
        return bool(cf_clearance)
    except Exception:
        return False


def _try_click_captcha(sb: SB, stage: str):
    try:
        sb.uc_gui_click_captcha()
        time.sleep(3)
    except Exception as e:
        print(f"⚠️ captcha 点击异常（{stage}）：{e}")


def _current_url(sb: SB) -> str:
    try:
        return (sb.get_current_url() or "").strip()
    except Exception:
        return ""


def _is_on_server_page(sb: SB, server_id: str) -> bool:
    url = _current_url(sb).lower()
    return f"/admin/servers/{server_id}" in url


def _is_on_userveria_authorize(sb: SB) -> bool:
    return "userveria.com/authorize" in _current_url(sb).lower()


def _is_on_create_account_page(sb: SB) -> bool:
    """userveria 在邮箱不存在/大小写不对时，会跳到 'Create your account' 页面。"""
    try:
        html = sb.get_page_source() or ""
    except Exception:
        return False
    return (
        "Create your account" in html
        and 'name="password_confirmation"' in html
    )


# ---------- 登录步骤 ----------
def _do_email_step(sb: SB, email: str) -> bool:
    email_sel = _first_visible(sb, EMAIL_SELECTORS, timeout_each=3)
    if not email_sel:
        return False
    try:
        sb.clear(email_sel)
        sb.type(email_sel, email)
    except Exception:
        return False
    btn_sel = _first_visible(sb, CONTINUE_BTN_SELECTORS, timeout_each=2)
    try:
        if btn_sel:
            sb.click(btn_sel)
        else:
            sb.send_keys(email_sel, "\n")
    except Exception:
        return False
    time.sleep(3)
    return True


def _do_password_step(sb: SB, password: str) -> bool:
    pwd_sel = _first_visible(sb, PASSWORD_SELECTORS, timeout_each=20)
    if not pwd_sel:
        return False
    try:
        sb.clear(pwd_sel)
        sb.type(pwd_sel, password)
    except Exception:
        return False
    btn_sel = _first_visible(sb, PASSWORD_SUBMIT_SELECTORS, timeout_each=3)
    try:
        if btn_sel:
            sb.click(btn_sel)
        else:
            sb.send_keys(pwd_sel, "\n")
    except Exception:
        return False
    time.sleep(4)
    return True


def _try_oauth_consent(sb: SB, server_id: str) -> bool:
    """
    OAuth 授权同意页处理：如果停在 userveria.com/authorize 上，尝试点 Authorize/Allow。
    """
    if not _is_on_userveria_authorize(sb):
        return False

    print("🔐 检测到 OAuth authorize 页，尝试点击同意按钮...")
    dump_html(sb, f"02b_authorize_{int(time.time())}.html")
    screenshot(sb, f"02b_authorize_{int(time.time())}.png")

    sel = _first_visible(sb, AUTHORIZE_BTN_SELECTORS, timeout_each=3)
    if sel:
        try:
            print(f"🔘 点击同意按钮：{sel}")
            sb.scroll_to(sel)
            time.sleep(0.3)
            sb.click(sel)
            time.sleep(4)
            return True
        except Exception as e:
            print(f"⚠️ 同意按钮点击失败：{e}")

    # 兜底：在 authorize 页上找所有 form 提交一遍
    try:
        forms = sb.find_elements("form")
        print(f"🔎 authorize 页上找到 {len(forms)} 个 form，尝试提交第一个")
        if forms:
            try:
                sb.execute_script("arguments[0].submit();", forms[0])
                time.sleep(4)
                return True
            except Exception as e:
                print(f"⚠️ form.submit() 失败：{e}")
    except Exception:
        pass

    return False


def _logout(sb: SB) -> bool:
    sel = _first_visible(sb, LOGOUT_LINK_SELECTORS, timeout_each=2)
    if sel:
        try:
            sb.scroll_to(sel)
            time.sleep(0.3)
            sb.click(sel)
            time.sleep(3)
            if _first_visible(sb, EMAIL_SELECTORS, timeout_each=3):
                return True
            url_now = _current_url(sb).lower()
            if "/admin/" not in url_now:
                return True
        except Exception:
            pass

    for url in LOGOUT_URL_CANDIDATES:
        try:
            sb.open(url)
            time.sleep(3)
            if _first_visible(sb, EMAIL_SELECTORS, timeout_each=3):
                return True
            url_now = _current_url(sb).lower()
            if "/admin/" not in url_now:
                return True
        except Exception:
            continue
    return False


def login_then_flow_one_account(
    email: str, password: str, server_id: str
) -> Tuple[str, bool, str, Optional[str], bool]:
    server_url = SERVER_URL_TPL.format(server_id=server_id)

    with SB(uc=True, locale="en", test=True) as sb:
        print("🚀 浏览器启动（UC Mode）")

        sb.uc_open_with_reconnect(server_url, reconnect_time=5.0)
        time.sleep(2)
        _try_click_captcha(sb, "访问控制台前")

        if _is_on_server_page(sb, server_id):
            print("✅ 已处于登录状态，直接进入 server 页")
        else:
            screenshot(sb, f"01_email_page_{int(time.time())}.png")
            dump_html(sb, f"01_email_page_{int(time.time())}.html")
            if not _do_email_step(sb, email):
                screenshot(sb, f"email_step_failed_{int(time.time())}.png")
                dump_html(sb, f"email_step_failed_{int(time.time())}.html")
                return "FAIL", _has_cf_clearance(sb), _current_url(sb), server_id, False

            _try_click_captcha(sb, "邮箱提交后")

            # 关键：如果跳到了 userveria 的 "Create your account" 页面，
            # 说明 userveria 这边查不到这个邮箱（大概率是 email 大小写不一致）。
            # 此时绝不能填密码 + 提交，会被识别为创建新账号。直接报错退出。
            if _is_on_create_account_page(sb):
                screenshot(sb, f"create_account_detected_{int(time.time())}.png")
                dump_html(sb, f"create_account_detected_{int(time.time())}.html")
                print(
                    "❌ 检测到 userveria 'Create your account' 页面：\n"
                    "   说明这个邮箱在 userveria 里不存在，最常见原因是\n"
                    "   ACCOUNTS_BATCH secret 里邮箱大小写写错了。\n"
                    "   请改成你注册时实际使用的大小写形式后重试。"
                )
                return "FAIL", _has_cf_clearance(sb), _current_url(sb), server_id, False

            screenshot(sb, f"02_password_page_{int(time.time())}.png")
            dump_html(sb, f"02_password_page_{int(time.time())}.html")
            if not _do_password_step(sb, password):
                screenshot(sb, f"password_step_failed_{int(time.time())}.png")
                dump_html(sb, f"password_step_failed_{int(time.time())}.html")
                return "FAIL", _has_cf_clearance(sb), _current_url(sb), server_id, False

            _try_click_captcha(sb, "密码提交后")

            # 等回到 searcade 服务器页；中间如果停在 OAuth authorize 页，尝试点同意
            ok = False
            for i in range(30):
                if _is_on_server_page(sb, server_id):
                    ok = True
                    break

                # 如果停在 userveria/authorize -> 试点同意按钮
                if _is_on_userveria_authorize(sb):
                    _try_oauth_consent(sb, server_id)
                    time.sleep(2)
                    if _is_on_server_page(sb, server_id):
                        ok = True
                        break

                # 如果回到 searcade.com 但不在 server 页，主动进 server 页
                cur = _current_url(sb).lower()
                if "searcade.com" in cur and "/admin/servers/" not in cur and "userveria" not in cur:
                    try:
                        sb.open(server_url)
                        time.sleep(3)
                        if _is_on_server_page(sb, server_id):
                            ok = True
                            break
                    except Exception:
                        pass

                time.sleep(1)

            if not ok:
                screenshot(sb, f"post_login_not_on_server_{int(time.time())}.png")
                dump_html(sb, f"post_login_not_on_server_{int(time.time())}.html")
                return "FAIL", _has_cf_clearance(sb), _current_url(sb), server_id, False

        screenshot(sb, f"03_server_page_{int(time.time())}.png")
        stay1 = random.randint(4, 6)
        print(f"⏳ 服务器页停留 {stay1} 秒...")
        time.sleep(stay1)

        try:
            print(f"↩️ 返回首页：{HOME_URL}")
            sb.open(HOME_URL)
            sb.wait_for_element_visible("body", timeout=30)
        except Exception:
            screenshot(sb, f"back_home_failed_{int(time.time())}.png")
            return "OK", _has_cf_clearance(sb), _current_url(sb), server_id, False

        stay2 = random.randint(3, 5)
        print(f"⏳ 首页停留 {stay2} 秒...")
        time.sleep(stay2)
        screenshot(sb, f"04_home_page_{int(time.time())}.png")

        logout_ok = _logout(sb)
        screenshot(sb, f"05_after_logout_{int(time.time())}.png")

        has_cf = _has_cf_clearance(sb)
        return "OK", has_cf, _current_url(sb), server_id, logout_ok


def main():
    accounts = build_accounts_from_env()
    display = setup_xvfb()

    ok = 0
    fail = 0
    logout_ok_count = 0
    tg_dests = set()

    try:
        for i, acc in enumerate(accounts, start=1):
            email = acc["email"]
            password = acc["password"]
            server_id = acc.get("server_id") or DEFAULT_SERVER_ID
            tg_token = (acc.get("tg_token") or "").strip()
            tg_chat = (acc.get("tg_chat") or "").strip()
            if tg_token and tg_chat:
                tg_dests.add((tg_token, tg_chat))
            safe_email = mask_email_keep_domain(email)

            print("\n" + "=" * 70)
            print(f"👤 [{i}/{len(accounts)}] 账号：{safe_email}  server_id={server_id}")
            print("=" * 70)

            try:
                status, has_cf, url_now, server_id_used, logout_ok = login_then_flow_one_account(
                    email, password, server_id
                )

                if status == "OK":
                    ok += 1
                    if logout_ok:
                        logout_ok_count += 1
                    msg = (
                        f"✅ searcade 登录成功\n"
                        f"账号：{safe_email}\n"
                        f"server_id：{server_id_used}\n"
                        f"退出：{'✅ 成功' if logout_ok else '❌ 失败'}\n"
                        f"当前页：{url_now}\n"
                        f"cf_clearance：{'OK' if has_cf else 'NONE'}"
                    )
                else:
                    fail += 1
                    msg = (
                        f"❌ searcade 登录失败\n"
                        f"账号：{safe_email}\n"
                        f"server_id：{server_id_used}\n"
                        f"当前页：{url_now}\n"
                        f"cf_clearance：{'OK' if has_cf else 'NONE'}"
                    )

                print(msg)
                tg_send(msg, tg_token, tg_chat)

            except Exception as e:
                fail += 1
                msg = f"❌ searcade 脚本异常\n账号：{safe_email}\n错误：{e}"
                print(msg)
                tg_send(msg, tg_token, tg_chat)

            time.sleep(5)
            if i < len(accounts):
                time.sleep(5)

        summary = f"📌 本次批量完成：登录成功 {ok} / 失败 {fail} | 退出成功 {logout_ok_count}/{ok}"
        print("\n" + summary)
        for token, chat in sorted(tg_dests):
            tg_send(summary, token, chat)

    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
