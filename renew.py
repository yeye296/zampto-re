#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from seleniumbase import SB

# ============================================================
# 基础配置
# ============================================================

SCRIPT_VERSION = "2026-08-23-wait25-tgshot-v1"

EMAIL = os.environ.get("ZAM_PTO_EMAIL", "").strip()
PASSWORD = os.environ.get("ZAM_PTO_PASSWORD", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()

IS_PROXY = os.environ.get("IS_PROXY", "true").strip().lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "http://127.0.0.1:1081").strip()

BASE_URL = "https://dash.zampto.net"
LOGIN_URL = f"{BASE_URL}/auth/login"
EMAIL_SELECTOR = "#email"
PASSWORD_SELECTOR = "#password"

# 强制限定在 20~30 秒，默认 25 秒。
try:
    LOGIN_STABILIZE_SECONDS = int(os.environ.get("LOGIN_STABILIZE_SECONDS", "25"))
except ValueError:
    LOGIN_STABILIZE_SECONDS = 25
LOGIN_STABILIZE_SECONDS = max(20, min(30, LOGIN_STABILIZE_SECONDS))

# ============================================================
# 通用工具
# ============================================================


def now_cn_str() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def mask_email(value: str) -> str:
    if "@" not in value:
        if not value:
            return "未配置"
        return value[:2] + "****"

    name, domain = value.split("@", 1)
    if len(name) <= 4:
        return f"{name}@{domain}"
    return f"{name[:2]}****{name[-2:]}@{domain}"


def safe_current_url(sb) -> str:
    try:
        return sb.get_current_url() or ""
    except Exception:
        return ""


def safe_title(sb) -> str:
    try:
        return sb.get_title() or ""
    except Exception:
        return ""


# ============================================================
# Telegram 通知：支持图片，图片失败自动退回文字消息
# ============================================================


def send_tg_message(
    status_icon: str,
    status_text: str,
    detail: str = "",
    photo_path: str | None = None,
) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        # print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return False

    text = (
        "🇫🇷 ZamPTO 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 续期账户: {mask_email(EMAIL)}\n"
        f"⏱️ 操作时间: {now_cn_str()}"
    )
    if detail:
        text += f"\n📝 详情: {detail[:700]}"

    # 优先发送截图。
    if photo_path:
        if os.path.isfile(photo_path):
            try:
                file_size = os.path.getsize(photo_path)
                print(f"📸 准备发送截图到 Telegram: {photo_path} ({file_size} bytes)")
                url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
                with open(photo_path, "rb") as fh:
                    response = requests.post(
                        url,
                        data={"chat_id": TG_CHAT_ID, "caption": text[:1000]},
                        files={
                            "photo": (
                                os.path.basename(photo_path),
                                fh,
                                "image/png",
                            )
                        },
                        timeout=30,
                    )

                if response.ok:
                    print("📩 Telegram 截图发送成功！")
                    return True

                print(
                    "⚠️ Telegram sendPhoto 失败: "
                    f"HTTP {response.status_code} - {response.text[:500]}"
                )
            except Exception as exc:
                print(f"⚠️ Telegram 截图发送异常: {exc}")
        else:
            print(f"⚠️ 截图文件不存在，改为发送纯文字: {photo_path}")

    # 图片失败或没有图片时，退回文字。
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        response = requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )
        if response.ok:
            print("📩 Telegram 文字通知发送成功！")
            return True

        print(
            "⚠️ Telegram sendMessage 失败: "
            f"HTTP {response.status_code} - {response.text[:500]}"
        )
    except Exception as exc:
        print(f"⚠️ Telegram 文字发送异常: {exc}")

    return False


def save_screenshot(sb, path: str) -> bool:
    try:
        result = sb.save_screenshot(path)
        exists = os.path.isfile(path)
        if exists:
            print(f"📸 截图已保存: {path} ({os.path.getsize(path)} bytes)")
        else:
            print(f"⚠️ save_screenshot 返回 {result!r}，但文件不存在: {path}")
        return exists
    except Exception as exc:
        print(f"⚠️ 保存截图失败 {path}: {exc}")
        return False


def notify_login_failure(sb, reason: str, screenshot_name: str) -> None:
    url = safe_current_url(sb)
    title = safe_title(sb)
    detail = reason
    if url:
        detail += f" | URL={url}"
    if title:
        detail += f" | Title={title}"

    if save_screenshot(sb, screenshot_name):
        send_tg_message("❌", "登录失败", detail, screenshot_name)
    else:
        send_tg_message("❌", "登录失败", detail)


# ============================================================
# Cloudflare Turnstile
# ============================================================

# 这里全部使用 raw string，避免 Python 对 JavaScript 中的 \d 产生
# SyntaxWarning: invalid escape sequence。
_TURNSTILE_EXISTS_JS = r"""
(function () {
    return !!document.querySelector('input[name="cf-turnstile-response"]') ||
           !!document.querySelector('iframe[src*="challenges.cloudflare"]') ||
           !!document.querySelector('.cf-turnstile');
})();
"""

_TURNSTILE_SOLVED_JS = r"""
(function () {
    var input = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(input && input.value && input.value.length > 20);
})();
"""

_TURNSTILE_EXPAND_JS = r"""
(function () {
    var input = document.querySelector('input[name="cf-turnstile-response"]');
    if (input) {
        var el = input;
        for (var i = 0; i < 20; i++) {
            el = el.parentElement;
            if (!el) break;
            var style = window.getComputedStyle(el);
            if (
                style.overflow === 'hidden' ||
                style.overflowX === 'hidden' ||
                style.overflowY === 'hidden'
            ) {
                el.style.overflow = 'visible';
            }
        }
    }

    document.querySelectorAll('iframe').forEach(function (frame) {
        if (frame.src && frame.src.includes('challenges.cloudflare')) {
            frame.style.width = '320px';
            frame.style.height = '75px';
            frame.style.minWidth = '320px';
            frame.style.display = 'block';
            frame.style.visibility = 'visible';
            frame.style.opacity = '1';
        }
    });

    return true;
})();
"""


def turnstile_exists(sb) -> bool:
    try:
        return bool(sb.execute_script(_TURNSTILE_EXISTS_JS))
    except Exception:
        return False


def turnstile_solved(sb) -> bool:
    try:
        return bool(sb.execute_script(_TURNSTILE_SOLVED_JS))
    except Exception:
        return False


def handle_turnstile(sb, max_attempts: int = 6, timeout_per_attempt: int = 10) -> bool:
    print(
        "🔍 处理 Cloudflare Turnstile 验证"
        f"（最多 {max_attempts} 次，每次 {timeout_per_attempt} 秒超时）..."
    )

    # 先给挑战组件一点时间完成渲染。
    time.sleep(2)

    if turnstile_solved(sb):
        print("✅ Turnstile 已静默通过")
        return True

    for _ in range(3):
        try:
            sb.execute_script(_TURNSTILE_EXPAND_JS)
        except Exception:
            pass
        time.sleep(0.5)

    for attempt in range(1, max_attempts + 1):
        if turnstile_solved(sb):
            print(f"✅ Turnstile 在第 {attempt} 次尝试前已经完成")
            return True

        print(f"🖱️ 第 {attempt}/{max_attempts} 次调用 uc_gui_click_captcha...")
        started = time.time()

        try:
            sb.uc_gui_click_captcha()
        except Exception as exc:
            print(f"⚠️ uc_gui_click_captcha 调用异常: {exc}")

        while time.time() - started < timeout_per_attempt:
            time.sleep(0.5)
            if turnstile_solved(sb):
                elapsed = time.time() - started
                print(f"✅ Turnstile 通过（第 {attempt} 次，耗时 {elapsed:.1f} 秒）")
                return True

        print(f"⏱️ 第 {attempt} 次尝试超时（{timeout_per_attempt} 秒）")

        # 下一次尝试前再把 iframe 展开一次。
        try:
            sb.execute_script(_TURNSTILE_EXPAND_JS)
        except Exception:
            pass
        time.sleep(1)

    print(f"❌ Turnstile {max_attempts} 次均失败或超时")
    return False


# ============================================================
# 页面辅助
# ============================================================


def read_alert(sb) -> str:
    selectors = [
        "div.alert",
        "[role='alert']",
        ".text-destructive",
        ".text-red-500",
        ".text-red-400",
    ]
    for selector in selectors:
        try:
            for element in sb.find_elements(selector):
                text = (element.text or "").strip()
                if text:
                    return text
        except Exception:
            pass
    return ""


def parse_duration_to_minutes(value: str) -> int | None:
    if not value:
        return None

    text = value.strip()
    if text.lower() == "expired":
        return 0

    day_match = re.search(r"(\d+)d", text, re.IGNORECASE)
    hour_match = re.search(r"(\d+)h", text, re.IGNORECASE)
    minute_match = re.search(r"(\d+)m", text, re.IGNORECASE)

    if not (day_match or hour_match or minute_match):
        return None

    days = int(day_match.group(1)) if day_match else 0
    hours = int(hour_match.group(1)) if hour_match else 0
    minutes = int(minute_match.group(1)) if minute_match else 0
    return days * 1440 + hours * 60 + minutes


def extract_remaining_minutes(sb) -> int | None:
    try:
        page_source = sb.get_page_source()
    except Exception as exc:
        print(f"⚠️ 获取页面源码失败: {exc}")
        return None

    if re.search(
        r"Expiry\s*\(Next Renewal\).*?Expired",
        page_source,
        re.IGNORECASE | re.DOTALL,
    ):
        print("⚠️ 检测到服务器已过期（Expired）")
        return 0

    span_match = re.search(
        r"Expiry\s*\(Next Renewal\).*?<span[^>]*>\s*"
        r"((?:\d+d\s*)?(?:\d+h\s*)?(?:\d+m\s*)?)\s*</span>",
        page_source,
        re.IGNORECASE | re.DOTALL,
    )
    if span_match:
        minutes = parse_duration_to_minutes(span_match.group(1))
        if minutes is not None:
            print(f"✅ 成功提取时间（HTML）: {minutes} 分钟")
            return minutes

    # raw string：不会再出现 invalid escape sequence '\d' 警告。
    js_extract = r"""
(function () {
    var spans = document.querySelectorAll(
        'span.font-medium.text-foreground, span.text-foreground, span'
    );

    for (var i = 0; i < spans.length; i++) {
        var text = (spans[i].textContent || '').trim();
        if (/^(?:\d+d\s*)?(?:\d+h\s*)?(?:\d+m\s*)$/.test(text) && /\d+[dhm]/.test(text)) {
            return text;
        }
    }

    var bodyText = (document.body && document.body.innerText) || '';
    if (/Expiry\s*\(Next Renewal\)[\s\S]{0,300}Expired/i.test(bodyText)) {
        return 'Expired';
    }

    return null;
})();
"""

    try:
        value = sb.execute_script(js_extract)
        minutes = parse_duration_to_minutes(value or "")
        if minutes is not None:
            print(f"✅ 成功提取时间（JavaScript）: {minutes} 分钟")
            return minutes
    except Exception as exc:
        print(f"⚠️ JavaScript 提取剩余时间失败: {exc}")

    print("⚠️ 所有剩余时间提取方法均失败")
    return None


def format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    if minutes == 0:
        return "Expired"
    days = minutes // 1440
    hours = (minutes % 1440) // 60
    mins = minutes % 60
    return f"{days}d {hours}h {mins}m"


# ============================================================
# 登录
# ============================================================


def login(sb) -> bool:
    print("\n" + "#" * 25)
    print("   开始 ZamPTO 登录")
    print("#" * 25)
    print(f"🌐 打开登录页面: {LOGIN_URL}")

    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
    except Exception as exc:
        print(f"❌ 打开登录页面失败: {exc}")
        # 页面甚至没打开时截图可能失败，但仍尝试发通知。
        notify_login_failure(sb, f"打开登录页失败: {exc}", "login_open_fail.png")
        return False

    print("⏳ 等待登录表单加载……")
    try:
        sb.wait_for_element_visible(EMAIL_SELECTOR, timeout=30)
        sb.wait_for_element_visible(PASSWORD_SELECTOR, timeout=30)
        print("✅ 登录表单加载成功")
    except Exception as exc:
        print(f"❌ 登录表单未加载成功: {exc}")
        notify_login_failure(
            sb,
            f"登录表单未加载成功: {exc}",
            "login_form_fail.png",
        )
        return False

    # --------------------------------------------------------
    # 关键修改：表单出现后，任何下一步操作之前，强制等待 20~30 秒。
    # 默认固定 25 秒，可通过 LOGIN_STABILIZE_SECONDS 修改。
    # --------------------------------------------------------
    print(
        f"🕒 登录表单已就绪，强制等待 {LOGIN_STABILIZE_SECONDS} 秒，"
        "让页面与验证组件完整加载……"
    )
    for remaining in range(LOGIN_STABILIZE_SECONDS, 0, -5):
        print(f"   ⏳ 还需等待约 {remaining} 秒...")
        time.sleep(min(5, remaining))
    print("✅ 强制等待结束，开始下一步操作")

    # Cookie 同意按钮。
    try:
        for button in sb.find_elements("button"):
            text = (button.text or "").strip().lower()
            if text in {"accept", "accept all", "同意", "接受", "allow all"}:
                button.click()
                print("🍪 已处理 Cookie 同意按钮")
                time.sleep(1)
                break
    except Exception:
        pass

    try:
        print(f"📧 填写邮箱 ({EMAIL_SELECTOR})……")
        sb.update_text(EMAIL_SELECTOR, EMAIL)
        time.sleep(0.5)

        print(f"🔑 填写密码 ({PASSWORD_SELECTOR})……")
        sb.update_text(PASSWORD_SELECTOR, PASSWORD)
        time.sleep(1)
    except Exception as exc:
        print(f"❌ 填写登录信息失败: {exc}")
        notify_login_failure(
            sb,
            f"填写账号密码失败: {exc}",
            "login_input_fail.png",
        )
        return False

    # 登录前先检查挑战是否存在。
    if turnstile_exists(sb):
        print("🛡️ 检测到 Turnstile 验证，开始处理...")
        if not handle_turnstile(sb):
            print("❌ 登录阶段 Turnstile 未通过")
            notify_login_failure(
                sb,
                "登录阶段 Turnstile 三次均未通过或超时",
                "login_turnstile_fail.png",
            )
            return False
    else:
        print("ℹ️ 登录提交前未检测到 Turnstile")

    print("🖱️ 敲击回车提交表单...")
    try:
        sb.press_keys(PASSWORD_SELECTOR, "\n")
    except Exception as exc:
        print(f"⚠️ 回车提交失败，尝试点击 Login 按钮: {exc}")
        try:
            clicked = sb.execute_script(
                r"""
(function () {
    var buttons = document.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
        var text = (buttons[i].textContent || '').trim().toLowerCase();
        if (text === 'login' || text === 'sign in') {
            buttons[i].click();
            return true;
        }
    }
    return false;
})();
"""
            )
            if not clicked:
                raise RuntimeError("找不到 Login/Sign in 按钮")
        except Exception as click_exc:
            notify_login_failure(
                sb,
                f"无法提交登录表单: {click_exc}",
                "login_submit_fail.png",
            )
            return False

    print("⏳ 等待登录结果……")
    login_paths = {"/auth/login", "/login"}

    for second in range(1, 46):
        time.sleep(1)

        current_url = safe_current_url(sb)
        path = ""
        try:
            path = urlparse(current_url).path.rstrip("/").lower()
        except Exception:
            pass

        if path and path not in login_paths:
            print("✅ 登录成功！")
            print(f"📄 当前 URL: {current_url}")
            print(f"📄 标题: {safe_title(sb)}")
            return True

        # 有些页面 URL 还没变，但登录表单已经消失。
        try:
            if (
                not sb.is_element_present(EMAIL_SELECTOR)
                and not sb.is_element_present(PASSWORD_SELECTOR)
            ):
                print("✅ 登录表单已消失，判定登录成功")
                return True
        except Exception:
            pass

        alert_text = read_alert(sb)
        if alert_text:
            lowered = alert_text.lower()
            print(f"📩 页面提示: {alert_text[:300]}")
            bad_keywords = (
                "invalid credential",
                "invalid credentials",
                "incorrect",
                "wrong password",
                "invalid password",
                "login failed",
            )
            if any(keyword in lowered for keyword in bad_keywords):
                print("❌ 页面明确提示账号或密码错误")
                notify_login_failure(
                    sb,
                    f"页面提示账号或密码错误: {alert_text}",
                    "login_bad_credentials.png",
                )
                return False

        # 如果提交以后才动态出现 Turnstile，只记录并尝试一次处理流程。
        # 这样避免“页面刚出现验证组件，脚本却只顾等 URL”的情况。
        if second in {3, 6, 10} and turnstile_exists(sb) and not turnstile_solved(sb):
            print("🛡️ 登录提交后检测到新的 Turnstile 验证")
            if handle_turnstile(sb):
                print("✅ 提交后 Turnstile 已通过，再提交一次登录表单")
                try:
                    sb.press_keys(PASSWORD_SELECTOR, "\n")
                except Exception:
                    pass
            else:
                print("⚠️ 提交后 Turnstile 仍未通过，继续等待最终结果")

        if second % 10 == 0:
            print(f"   ⏳ 已等待登录结果 {second} 秒...")

    print("❌ 登录超时（45 秒）")
    notify_login_failure(
        sb,
        "登录 45 秒仍未跳出登录页",
        "login_timeout.png",
    )
    return False


# ============================================================
# 获取服务器 ID
# ============================================================


def get_server_ids(sb) -> list[str]:
    print("🔍 正在提取服务器 ID 列表...")
    time.sleep(5)

    ids: list[str] = []

    try:
        page_source = sb.get_page_source()
        ids.extend(re.findall(r"ID:\s*(\d+)", page_source))
    except Exception as exc:
        print(f"⚠️ 正则提取服务器 ID 失败: {exc}")

    if not ids:
        try:
            for element in sb.find_elements("a[href*='/server?id=']"):
                href = element.get_attribute("href") or ""
                match = re.search(r"[?&]id=(\d+)", href)
                if match:
                    ids.append(match.group(1))
        except Exception as exc:
            print(f"⚠️ 链接提取服务器 ID 失败: {exc}")

    if not ids:
        current_url = safe_current_url(sb)
        match = re.search(r"[?&]id=(\d+)", current_url)
        if match:
            ids.append(match.group(1))

    # 保持顺序去重。
    unique_ids = list(dict.fromkeys(ids))
    if unique_ids:
        print(f"✅ 找到 {len(unique_ids)} 个服务器 ID: {unique_ids}")
    else:
        print("❌ 未能提取到任何服务器 ID")

    return unique_ids


# ============================================================
# 续期单个服务器
# ============================================================


def click_renew_button(sb) -> bool:
    click_script = r"""
(function () {
    var buttons = document.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
        var text = (buttons[i].textContent || '').trim();
        if (text === 'Renew Server') {
            buttons[i].scrollIntoView({behavior: 'smooth', block: 'center'});
            buttons[i].click();
            return true;
        }
    }
    return false;
})();
"""

    try:
        return bool(sb.execute_script(click_script))
    except Exception as exc:
        print(f"⚠️ JavaScript 点击 Renew Server 失败: {exc}")
        return False


def renew_one_server_by_id(sb, server_id: str, index: int) -> dict:
    result = {
        "index": index,
        "server_id": server_id,
        "status": "unknown",
        "detail": "",
    }

    detail_url = f"{BASE_URL}/server?id={server_id}"

    try:
        print(f"\n🔄 正在处理第 {index + 1} 个服务器: ID={server_id}")
        print(f"🌐 打开详情页: {detail_url}")
        sb.get(detail_url)

        print("⏳ 等待页面关键内容加载...")
        try:
            sb.wait_for_text("Server last renewed", timeout=15)
            print("✅ 检测到 'Server last renewed' 文字")
        except Exception:
            try:
                sb.wait_for_text("Expiry (Next Renewal)", timeout=10)
                print("✅ 检测到 'Expiry (Next Renewal)' 文字")
            except Exception:
                print("⚠️ 未检测到预期文字，但继续尝试")

        time.sleep(3)
        current_url = safe_current_url(sb)
        if "/server" not in current_url.lower():
            result["status"] = "failed"
            result["detail"] = f"未进入详情页，当前 URL={current_url}"
            save_screenshot(sb, f"server_page_fail_{server_id}.png")
            return result

        old_minutes = extract_remaining_minutes(sb)
        print(f"📅 原始剩余时间: {format_minutes(old_minutes)}")

        print("🖱️ 尝试点击 Renew Server 按钮...")
        if not click_renew_button(sb):
            result["status"] = "error"
            result["detail"] = "无法点击 Renew Server 按钮"
            path = f"renew_click_fail_{server_id}.png"
            save_screenshot(sb, path)
            send_tg_message("❌", f"服务器 {server_id} 续期失败", result["detail"], path)
            return result

        print("✅ Renew Server 已点击")
        print("⏳ 等待 5 秒后检查时间变化...")
        time.sleep(5)

        quick_minutes = extract_remaining_minutes(sb)
        if old_minutes is not None and quick_minutes is not None:
            delta = quick_minutes - old_minutes
            if delta > 1000 or (old_minutes == 0 and quick_minutes > 0):
                result["status"] = "success"
                result["detail"] = (
                    f"续期成功：{format_minutes(old_minutes)} -> "
                    f"{format_minutes(quick_minutes)}，增加 {delta} 分钟"
                )
                path = f"renew_success_{server_id}.png"
                save_screenshot(sb, path)
                send_tg_message("✅", f"服务器 {server_id} 续期成功", result["detail"], path)
                return result

        # 没有立即变化时检查验证组件。
        if turnstile_exists(sb) and not turnstile_solved(sb):
            print("🛡️ 续期阶段检测到 Turnstile 验证")
            if not handle_turnstile(sb):
                print("⚠️ 续期阶段 Turnstile 未通过，但仍继续刷新确认最终状态")

        print("⏳ 等待 5 秒并重新加载详情页确认最终状态...")
        time.sleep(5)
        sb.get(detail_url)
        try:
            sb.wait_for_text("Server last renewed", timeout=15)
        except Exception:
            pass
        time.sleep(3)

        new_minutes = extract_remaining_minutes(sb)
        print(f"📅 最终剩余时间: {format_minutes(new_minutes)}")

        alert_text = read_alert(sb)
        if alert_text:
            print(f"📩 页面提示: {alert_text[:300]}")

        if old_minutes is not None and new_minutes is not None:
            delta = new_minutes - old_minutes
            if delta > 1000 or (old_minutes == 0 and new_minutes > 0):
                result["status"] = "success"
                result["detail"] = (
                    f"续期成功：{format_minutes(old_minutes)} -> "
                    f"{format_minutes(new_minutes)}，增加 {delta} 分钟"
                )
            elif -10 < delta < 100:
                result["status"] = "skipped"
                result["detail"] = f"时间变化不明显：{delta} 分钟"
            else:
                result["status"] = "unknown"
                result["detail"] = f"时间变化异常：{delta} 分钟"
        elif alert_text and any(
            word in alert_text.lower() for word in ("renewed", "success", "extended")
        ):
            result["status"] = "success"
            result["detail"] = alert_text
        else:
            result["status"] = "unknown"
            result["detail"] = (
                f"无法确认续期结果，原={old_minutes}，新={new_minutes}"
            )

        screenshot_path = f"renew_result_{server_id}.png"
        save_screenshot(sb, screenshot_path)

        if result["status"] == "success":
            send_tg_message(
                "✅",
                f"服务器 {server_id} 续期成功",
                result["detail"],
                screenshot_path,
            )
        elif result["status"] == "skipped":
            send_tg_message(
                "⏭️",
                f"服务器 {server_id} 本次跳过/未变化",
                result["detail"],
                screenshot_path,
            )
        else:
            send_tg_message(
                "⚠️",
                f"服务器 {server_id} 续期状态异常",
                result["detail"],
                screenshot_path,
            )

        return result

    except Exception as exc:
        print(f"❌ 处理服务器 {server_id} 异常: {exc}")
        result["status"] = "error"
        result["detail"] = str(exc)
        path = f"renew_exception_{server_id}.png"
        save_screenshot(sb, path)
        send_tg_message(
            "❌",
            f"服务器 {server_id} 续期异常",
            result["detail"],
            path,
        )
        return result


# ============================================================
# 全部服务器续期
# ============================================================


def renew_all_servers_by_id(sb) -> list[dict]:
    print("\n" + "#" * 25)
    print("   开始 ZamPTO 自动续期流程（通过服务器 ID）")
    print("#" * 25)

    server_ids = get_server_ids(sb)
    if not server_ids:
        path = "no_servers.png"
        save_screenshot(sb, path)
        print("❌ 未获取到任何服务器 ID")
        send_tg_message("❌", "执行失败", "未获取到任何服务器 ID", path)
        raise SystemExit(1)
        # return []

    print(f"📋 待续期服务器 ID 列表: {server_ids}")
    results: list[dict] = []

    for index, server_id in enumerate(server_ids):
        result = renew_one_server_by_id(sb, server_id, index)
        results.append(result)
        print(
            f"📊 第 {index + 1} 个服务器 (ID={server_id}) "
            f"续期结果: {result['status']} - {result['detail']}"
        )

    total = len(results)
    success = sum(1 for item in results if item["status"] == "success")
    skipped = sum(1 for item in results if item["status"] == "skipped")
    failed = total - success - skipped

    summary = (
        f"续期完成：共 {total} 个服务器\n"
        f"✅ 成功: {success}\n"
        f"⏭️ 跳过: {skipped}\n"
        f"❌ 失败/未知: {failed}"
    )

    detail = "\n".join(
        f"#{item['index'] + 1} ID={item['server_id']}: "
        f"{item['status']} - {item['detail']}"
        for item in results
    )

    print("\n" + "=" * 50)
    print(summary)
    print("=" * 50)
    print(detail)
    print("=" * 50)
    if success == 0: raise SystemExit(1)
    return results


# ============================================================
# 主程序
# ============================================================


def main() -> None:
    print("#" * 40)
    print("   ZamPTO 自动登录续期")
    print(f"   版本: {SCRIPT_VERSION}")
    print("#" * 40)

    if not EMAIL or not PASSWORD:
        print("❌ 未配置 ZAM_PTO_EMAIL 或 ZAM_PTO_PASSWORD")
        send_tg_message("❌", "账号环境变量未配置")
        raise SystemExit(1)

    sb_kwargs = {
        "uc": True,
        "headless": False,
    }

    if IS_PROXY:
        print(f"🔗 使用 sing-box 本地代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🌐 未启用代理，使用直连")

    try:
        with SB(**sb_kwargs) as sb:
            try:
                sb.open("https://api.ip.sb/ip")
                exit_ip = sb.get_text("body").strip()
                print(f"📍 当前出口 IP: {exit_ip}")
            except Exception as exc:
                print(f"⚠️ 无法获取出口 IP: {exc}")
                if IS_PROXY:
                    send_tg_message("❌", "代理连接失败", str(exc))
                    raise SystemExit(1)

            if not login(sb):
                print("\n❌ 登录失败，终止续期操作。")
                # login() 内部已经发送带截图的 TG，避免这里重复发一条纯文字。
                raise SystemExit(1)

            print("\n🎉 登录流程成功")
            renew_all_servers_by_id(sb)

    except SystemExit:
        raise
    except Exception as exc:
        print(f"❌ 程序运行异常: {exc}")
        send_tg_message("❌", "程序运行异常", str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
