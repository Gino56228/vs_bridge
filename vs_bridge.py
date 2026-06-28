#!/usr/bin/env python3
"""
VS Bridge — VS Code Claude ↔ 小龙虾 (WeChat) 桥接脚本
================================================================
功能：
  1. 监控 VS Code 中 Claude 的状态（工作中 / 工作完 / 空闲）
  2. 检测到"工作完·待输入文本"时，截图 OCR 提取最后的 Thought for 内容，发微信
  3. 接收小龙虾微信指令，自动粘贴到 VS Code 输入框并点击发送
  4. 小龙虾可随时读取状态文件了解克劳德当前状态

用法：
  python vs_bridge.py                    # 主监控循环
  python vs_bridge.py --send "消息内容"   # 把消息粘贴到 VS Code 并发送
  python vs_bridge.py --status            # 快速查看当前状态
  python vs_bridge.py --ocr-test          # 截图全屏OCR测试（不发送）
"""

import pyautogui
import cv2
import numpy as np
import sys

# 修复 Windows GBK 终端编码问题
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from PIL import Image
import time
import subprocess
import os
import re
import json
import argparse
import glob
import ctypes
from ctypes import wintypes
from datetime import datetime

# ======================== Win32 窗口查找 ========================

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 缓存 VS Code 窗口位置（减少系统调用）
_vscode_window_cache = {"rect": None, "hwnd": None, "ts": 0}
WINDOW_CACHE_TTL = 5  # 秒


def find_vscode_windows():
    """查找所有可见的 VS Code 窗口，返回 [(hwnd, title, left, top, right, bottom), ...]"""
    windows = []

    def enum_callback(hwnd, lParam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value
                # VS Code 窗口标题通常包含 "Visual Studio Code" 或 "- Code"
                if any(kw in title for kw in ['Visual Studio Code', '- Code', 'Claude']):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    # 过滤掉太小的窗口（如托盘图标窗口）
                    ww = rect.right - rect.left
                    wh = rect.bottom - rect.top
                    if ww > 300 and wh > 300:
                        windows.append((hwnd, title, rect.left, rect.top, rect.right, rect.bottom))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_long, ctypes.c_long)
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return windows


def get_vscode_rect():
    """获取 VS Code 窗口的屏幕坐标（带缓存），返回 (left, top, right, bottom)"""
    now = time.time()
    cache = _vscode_window_cache
    if cache["rect"] is not None and (now - cache["ts"]) < WINDOW_CACHE_TTL:
        return cache["rect"]

    windows = find_vscode_windows()
    if not windows:
        if cache["rect"] is not None:
            return cache["rect"]  # 用过期缓存兜底
        return None

    # 优先选最大的窗口（通常是主窗口）
    best = max(windows, key=lambda w: (w[4]-w[2]) * (w[5]-w[3]))
    _, title, l, t, r, b = best
    rect = (l, t, r, b)
    cache["rect"] = rect
    cache["hwnd"] = best[0]
    cache["ts"] = now
    log(f"VS Code窗口: \"{title[:50]}\" @ ({l},{t})-({r},{b}) {r-l}x{b-t}")
    return rect


def bring_vscode_to_front():
    """把 VS Code 窗口放到前台"""
    rect = get_vscode_rect()
    if rect:
        cache = _vscode_window_cache
        hwnd = cache.get("hwnd")
        if hwnd:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.2)

# ======================== 配置 ========================
DESKTOP = r"E:\zhuomian"

# 模板图片路径
TEMPLATES = {
    "working":   os.path.join(DESKTOP, "工作中.png"),
    "done":      os.path.join(DESKTOP, "工作完·待输入文本.png"),
    "send_btn":  os.path.join(DESKTOP, "发送按钮.png"),
    "text_input": os.path.join(DESKTOP, "文本输入框.png"),
}

# 状态文件、命令文件、PID文件（单例锁）
STATUS_FILE  = os.path.join(DESKTOP, "vs_bridge_status.json")
COMMAND_FILE = os.path.join(DESKTOP, "vs_bridge_command.txt")
PID_FILE     = os.path.join(DESKTOP, "vs_bridge.pid")

# 已发送内容哈希（防止同一内容重复发送）
_last_content_hash = None

# 小龙虾 OpenClaw 发送接口
NODE_EXE = r"F:\Node.js\node.exe"
OPENCLAW_JS = r"F:\node_global\node_modules\openclaw\dist\index.js"
OPENCLAW_TARGET = "o9cq806lf2H-vDTJtymEBOG-nBjo@im.wechat"

# 轮询间隔（秒）
POLL_INTERVAL = 3
IDLE_INTERVAL = 5

# 模板匹配阈值 (0~1)
MATCH_THRESHOLD = 0.75

# OCR 引擎（延迟加载）
_reader = None


# ======================== 工具函数 ========================

def get_reader():
    """延迟加载 EasyOCR Reader（首次加载较慢）"""
    global _reader
    if _reader is None:
        print("[OCR] 初始化 EasyOCR (ch_sim+en) ...")
        _reader = __import__('easyocr').Reader(['ch_sim', 'en'], gpu=False)
        print("[OCR] 初始化完成")
    return _reader


def log(msg):
    """带时间戳的日志"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def write_status(state, detail=""):
    """写入状态文件供小龙虾读取"""
    data = {
        "state": state,
        "detail": detail,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"写状态文件失败: {e}")


def read_command():
    """读取并消费命令文件（小龙虾写入的指令）"""
    if not os.path.exists(COMMAND_FILE):
        return None
    try:
        with open(COMMAND_FILE, "r", encoding="utf-8") as f:
            cmd = f.read().strip()
        os.remove(COMMAND_FILE)
        return cmd if cmd else None
    except Exception as e:
        log(f"读命令文件失败: {e}")
        return None


def cv2_imread_unicode(filepath):
    """OpenCV imread 不支持中文路径，用 numpy 读取"""
    try:
        with open(filepath, "rb") as f:
            data = np.frombuffer(f.read(), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def cv2_imwrite_unicode(filepath, img_np):
    """OpenCV imwrite 不支持中文路径，用 PIL 保存"""
    try:
        rgb = cv2.cvtColor(img_np, cv2.COLOR_BGR2RGB)
        Image.fromarray(rgb).save(filepath)
        return True
    except Exception:
        return False


def template_match(screenshot_np, template_path, threshold=MATCH_THRESHOLD):
    """
    在截图中搜索模板图片。
    返回 (center_x, center_y) 或 None
    """
    if not os.path.exists(template_path):
        log(f"模板不存在: {template_path}")
        return None

    template = cv2_imread_unicode(template_path)
    if template is None:
        log(f"无法读取模板: {template_path}")
        return None

    t_h, t_w = template.shape[:2]
    s_h, s_w = screenshot_np.shape[:2]

    if t_h > s_h or t_w > s_w:
        return None

    result = cv2.matchTemplate(screenshot_np, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        return None

    center_x = max_loc[0] + t_w // 2
    center_y = max_loc[1] + t_h // 2
    return (center_x, center_y)


def screenshot_full():
    """全屏截图，返回 numpy BGR 数组"""
    img = pyautogui.screenshot()
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def screenshot_vscode():
    """
    只截图 VS Code 窗口区域。
    返回 (image_np, (window_left, window_top)) 或 (None, None)
    """
    rect = get_vscode_rect()
    if rect is None:
        return None, None
    l, t, r, b = rect
    # 确保坐标在屏幕范围内
    screen_w, screen_h = pyautogui.size()
    l = max(0, l)
    t = max(0, t)
    r = min(screen_w, r)
    b = min(screen_h, b)
    if r <= l or b <= t:
        return None, None
    img = pyautogui.screenshot(region=(l, t, r-l, b-t))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR), (l, t)


def screenshot_region(x1, y1, x2, y2):
    """区域截图（绝对屏幕坐标），返回 numpy BGR 数组"""
    img = pyautogui.screenshot(region=(x1, y1, x2 - x1, y2 - y1))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def ocr_text(image_np):
    """
    对图像进行 OCR，返回提取的全部文本。
    image_np: numpy BGR 或 RGB 数组
    """
    reader = get_reader()
    # easyocr 需要 RGB
    if len(image_np.shape) == 3 and image_np.shape[2] == 3:
        rgb = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)
    else:
        rgb = image_np
    results = reader.readtext(rgb, detail=0)
    return "\n".join(results)


def extract_last_thought(text):
    """
    从 OCR 文本中提取最后一个 "Thought for XXs >" 的内容。
    容错处理：OCR可能把 '>' 漏掉、把 '5' 误识别为 'S'。
    返回 {"thought": "Thought for 11s >", "content": "..."}  或 None
    """
    # 宽松匹配：Thought for + 数字(或OCR误读的S) + 可选s + 可选>
    # 关键是把 'S'/'s' 当 '5' 处理（OCR常见混淆）
    pattern = r'Thought\s+for\s+(\d+|S|s)\s*s?\s*>?'
    matches = list(re.finditer(pattern, text, re.IGNORECASE))

    if not matches:
        return None

    last_match = matches[-1]
    thought_raw = last_match.group(0).strip()
    seconds_str = last_match.group(1)

    # 修正OCR误读：'S'/'s' → '5'
    if seconds_str.upper() == 'S':
        seconds_str = '5'
    seconds = int(seconds_str)

    # 取 Thought for ... 之后的所有文字
    content = text[last_match.end():].strip()

    return {
        "thought": f"Thought for {seconds}s >",
        "seconds": seconds,
        "content": content,
    }


def send_to_wechat(message):
    """通过小龙虾 OpenClaw 发送文字到微信"""
    log(f"发微信: {message[:80]}...")
    try:
        result = subprocess.run(
            [NODE_EXE, OPENCLAW_JS, "message", "send",
             "--channel", "openclaw-weixin",
             "--target", OPENCLAW_TARGET,
             "--message", message],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=DESKTOP,
        )
        if result.returncode != 0:
            err = result.stderr.strip() if result.stderr else ""
            log(f"发送失败: {err}")
            return False
        log(f"发送成功: {result.stdout.strip()[:100]}")
        return True
    except subprocess.TimeoutExpired:
        log("发送超时")
        return False
    except Exception as e:
        log(f"发送异常: {e}")
        return False


def send_image_to_wechat(image_path, caption=""):
    """通过小龙虾发送图片到微信"""
    log(f"发图片: {image_path}")
    try:
        args = [
            NODE_EXE, OPENCLAW_JS, "message", "send",
            "--channel", "openclaw-weixin",
            "--target", OPENCLAW_TARGET,
            "--message", caption or "VS截图",
            "--media", image_path,
        ]
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=DESKTOP,
        )
        if result.returncode != 0:
            err = result.stderr.strip() if result.stderr else ""
            log(f"发图片失败: {err}")
            return False
        log("图片发送成功")
        return True
    except Exception as e:
        log(f"发图片异常: {e}")
        return False


# ======================== 转录文件读取（无需截图OCR） ========================

TRANSCRIPT_BASE = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def find_latest_transcript():
    """找到最新的 Claude 会话转录 JSONL 文件（自动适配所有项目目录）"""
    if not os.path.isdir(TRANSCRIPT_BASE):
        return None
    # 扫描所有项目子目录下的 jsonl 文件
    pattern = os.path.join(TRANSCRIPT_BASE, "*", "*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def extract_last_response(transcript_path):
    """
    从转录 JSONL 文件中提取最后一条 Claude 回复。
    返回 (thinking_text, response_text) — 可能为 None。
    对大文件只读最后 1MB 以提高性能。
    """
    try:
        file_size = os.path.getsize(transcript_path)
        with open(transcript_path, "r", encoding="utf-8") as f:
            if file_size > 1_000_000:
                # 大文件：只读尾部
                f.seek(file_size - 1_000_000)
                f.readline()  # 丢弃不完整的第一行
                text = f.read()
                lines = text.splitlines()
            else:
                lines = f.readlines()
    except Exception as e:
        log(f"读取转录文件失败: {e}")
        return None, None

    last_thinking = None
    last_text = None

    # 从后往前扫描，找最后一条 assistant 消息
    for i in range(len(lines) - 1, -1, -1):
        try:
            data = json.loads(lines[i])
        except Exception:
            continue

        if data.get("type") != "assistant":
            continue

        msg = data.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "thinking" and last_thinking is None:
                last_thinking = item.get("thinking", "")
            if t == "text" and last_text is None:
                last_text = item.get("text", "")

        if last_thinking or last_text:
            break

    return last_thinking, last_text


def do_capture(done_pos=None):
    """
    读取转录文件 → 提取最后 Claude 回复 → 发微信。
    替代了原来的截图+OCR方案，100%准确，瞬间完成。
    """
    global _last_content_hash

    log("=== 读取转录文件 ===")

    transcript = find_latest_transcript()
    if not transcript:
        log("未找到转录文件")
        return False

    log(f"转录文件: {os.path.basename(transcript)} ({os.path.getsize(transcript)} bytes)")

    # 稍等确保转录文件已完全写入
    time.sleep(3)

    thinking, text = extract_last_response(transcript)

    if thinking:
        msg = f"【克劳德完成】\n{thinking}"
        if text:
            msg += f"\n\n---\n{text}"
    elif text:
        msg = f"【克劳德完成】\n{text}"
    else:
        log("未提取到内容")
        return False

    # 内容去重：相同内容不重复发送
    import hashlib
    content_hash = hashlib.md5(msg.encode()).hexdigest()
    if content_hash == _last_content_hash:
        log("内容与上次相同，跳过重复发送")
        return False
    _last_content_hash = content_hash

    log(f"提取到: thinking={len(thinking or '')}字, text={len(text or '')}字")
    send_to_wechat(msg)
    log("=== 完成 ===")
    return True


# ======================== 图片识别（模板匹配 + 截图） ========================

def detect_status(screen_np):
    """
    检测 VS Code Claude 当前状态。
    返回: "working" | "done" | "idle"
    """
    # 先检测"工作完·待输入文本"（优先级更高）
    done_pos = template_match(screen_np, TEMPLATES["done"], MATCH_THRESHOLD)
    if done_pos:
        return "done", done_pos

    # 再检测"工作中"
    work_pos = template_match(screen_np, TEMPLATES["working"], MATCH_THRESHOLD)
    if work_pos:
        return "working", work_pos

    return "idle", None


def paste_and_send(text):
    """
    在 VS Code 中粘贴文字并点击发送。
    优先用发送按钮锚定位置，回退到输入框模板匹配。
    """
    log(f"准备发送: {text[:60]}...")

    win_img, (win_l, win_t) = screenshot_vscode()
    if win_img is None:
        log("未找到 VS Code 窗口")
        return False

    def to_screen(rel_pos):
        return (win_l + rel_pos[0], win_t + rel_pos[1])

    # 1. 优先用输入框模板匹配，回退到发送按钮推算（实测输入框在发送按钮左边约320px）
    input_rel = template_match(win_img, TEMPLATES["text_input"], MATCH_THRESHOLD - 0.25)
    send_rel = template_match(win_img, TEMPLATES["send_btn"], MATCH_THRESHOLD)

    if input_rel:
        click_rel = input_rel
        log(f"输入框模板匹配: ({click_rel[0]},{click_rel[1]})")
    elif send_rel:
        sx, sy = send_rel
        click_rel = (sx - 320, sy)
        log(f"用发送按钮({sx},{sy})推算输入框({click_rel[0]},{click_rel[1]})")
    else:
        log("未找到发送按钮和输入框")
        return False

    # 2. 点击输入框
    click_screen = to_screen(click_rel)
    pyautogui.click(click_screen[0], click_screen[1])
    time.sleep(0.3)

    # 3. 全选 + 粘贴
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)

    # 4. 点击发送按钮
    if send_rel:
        send_screen = to_screen(send_rel)
    else:
        # 重新截图找发送按钮
        win_img2, (wl2, wt2) = screenshot_vscode()
        if win_img2 is not None:
            send_rel2 = template_match(win_img2, TEMPLATES["send_btn"], MATCH_THRESHOLD)
            if send_rel2:
                send_screen = (wl2 + send_rel2[0], wt2 + send_rel2[1])
            else:
                log("未找到发送按钮")
                return False
        else:
            return False

    log(f"点击发送按钮 {send_screen}")
    pyautogui.click(send_screen[0], send_screen[1])
    time.sleep(0.3)

    # 发送完鼠标往右移，不挡住左边模板图标（只移这一次）
    pyautogui.moveRel(30, 0)

    log("发送完成")
    return True


def send_text_to_vscode(text):
    """把文字复制到剪贴板，然后粘贴发送到 VS Code Claude"""
    import pyperclip
    # 复制到剪贴板
    pyperclip.copy(text)
    log(f"已复制到剪贴板: {text[:60]}...")
    # 粘贴并发送
    return paste_and_send(text)


def find_claude_chat_region(screen_np, icon_pos):
    """
    根据状态图标位置（最可靠的锚点），计算 Claude 对话内容区域。
    图标在 Claude 面板右下角，对话内容在图标上方和左侧。
    """
    sh, sw = screen_np.shape[:2]
    cx, cy = icon_pos

    # Claude 对话内容就在状态图标正上方。
    # 图标在 Claude 面板右下角，向左650px是面板左边界。
    # 向上300px范围内的文字就是最新几条消息（含 Thought for）。
    panel_left = max(0, cx - 650)
    panel_right = min(sw, cx + 80)

    # 截图标上方350px — 刚好覆盖最后1-2条完整回复
    content_bottom = cy - 60   # 图标上方留白，避开状态图标本身
    content_top = max(0, content_bottom - 350)

    pw = panel_right - panel_left
    ph = content_bottom - content_top
    if pw < 300: panel_right = panel_left + 400
    if ph < 150: content_top = content_bottom - 500

    log(f"Claude面板区域: ({panel_left},{content_top}) → ({panel_right},{content_bottom}) [{pw}x{ph}]")
    return panel_left, content_top, panel_right, content_bottom


def preprocess_for_ocr(image_np, max_size=1200):
    """
    图像预处理：缩小以加速OCR，保持彩色信息。
    max_size: 长边最大像素。
    """
    h, w = image_np.shape[:2]
    scale = min(1.0, max_size / max(h, w))
    if scale < 1.0:
        nw, nh = int(w * scale), int(h * scale)
        image_np = cv2.resize(image_np, (nw, nh), interpolation=cv2.INTER_AREA)
    # 保持BGR彩色给EasyOCR（彩色对中文识别有帮助）
    return image_np


# ======================== 主循环 ========================

# 已处理标记（防止同一轮完成重复发送）
_done_sent = False


def ensure_single_instance():
    """确保只有一个桥实例在运行，旧实例自动清理"""
    my_pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid != my_pid:
                import signal
                try:
                    os.kill(old_pid, signal.SIGTERM)
                except Exception:
                    pass
        except Exception:
            pass
    with open(PID_FILE, "w") as f:
        f.write(str(my_pid))


def main_loop():
    """
    主循环 — 按阿江的流程：
    空闲（安静等待命令）→ 收到命令 → 粘贴发送 → 移鼠标 →
    监控工作状态 → 检测完成 → 转发微信 → 回到空闲
    """
    global _vscode_window_cache, _done_sent, _last_content_hash

    ensure_single_instance()

    log("══════════════════════════════════════")
    log("VS Bridge 启动 — 空闲中，等待命令...")
    log("══════════════════════════════════════")

    write_status("暂无工作在进行", "空闲中，等待命令...")

    while True:
        try:
            # ── 空闲状态：只检查命令文件，不截图不动鼠标 ──
            cmd = read_command()
            if not cmd:
                time.sleep(2)
                continue

            log(f"收到命令: {cmd[:80]}...")

            # ── 粘贴发送 ──
            if not send_text_to_vscode(cmd):
                log("发送失败，回到空闲")
                write_status("暂无工作在进行", "发送失败，回到空闲")
                continue

            # ── 监控模式：轮询工作状态 ──
            write_status("正工作", "等待克劳德开始...")
            _done_sent = False
            prev_state = "idle"
            saw_working = False
            idle_count = 0
            poll_count = 0
            MONITOR_TIMEOUT = 40  # 最多监控40轮（~2分钟）

            while True:
                time.sleep(POLL_INTERVAL)
                poll_count += 1

                # 截图检测状态（监控期间才截图）
                _vscode_window_cache["ts"] = 0
                win_img, _ = screenshot_vscode()
                if win_img is None:
                    continue

                state, pos = detect_status(win_img)

                if state != prev_state:
                    log(f"状态: {prev_state} → {state}")
                    prev_state = state

                if state == "working":
                    saw_working = True
                    write_status("正工作", "克劳德正在生成回复...")

                elif state == "done":
                    if not _done_sent:
                        _done_sent = True
                        write_status("任务完成", "正在读取转录文件...")
                        log("检测到「工作完·待输入文本」")
                        do_capture(pos)
                        write_status("空闲中", "回复已发送")
                        break  # 退出监控，回到空闲

                elif state == "idle":
                    if saw_working and not _done_sent:
                        # working→idle 可能是漏了 done
                        idle_count += 1
                        if idle_count >= 3:
                            _done_sent = True
                            write_status("任务完成", "working→idle兜底触发")
                            log("working→idle（可能漏了done），兜底读取转录")
                            do_capture(None)
                            write_status("空闲中", "回复已发送")
                            break
                    else:
                        idle_count = 0

                # 超时保护（~2分钟）
                if poll_count >= MONITOR_TIMEOUT and not _done_sent:
                    log(f"监控超时({poll_count}轮)，兜底触发")
                    _done_sent = True
                    do_capture(None)
                    write_status("空闲中", "超时兜底")
                    break

            # ── 回到空闲 ──
            write_status("暂无工作在进行", "空闲中，等待命令...")
            log("回到空闲状态")

        except KeyboardInterrupt:
            log("用户中断，退出。")
            write_status("已停止", "VS Bridge 已退出")
            break
        except Exception as e:
            log(f"异常: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(IDLE_INTERVAL)


def quick_status():
    """快速状态检查"""
    win_img, (win_l, win_t) = screenshot_vscode()
    if win_img is None:
        print("未找到 VS Code 窗口")
        return "idle"
    state, pos = detect_status(win_img)

    labels = {
        "working": "正工作 [WORKING]",
        "done": "任务完成 [DONE]",
        "idle": "暂无工作在进行 [IDLE]",
    }
    print(f"当前状态: {labels.get(state, state)}")
    if pos:
        print(f"窗口内位置: {pos}")
    return state


def ocr_test():
    """测试：读取转录文件 → 提取最后回复"""
    log("=== 转录文件读取测试 ===")

    transcript = find_latest_transcript()
    if not transcript:
        log("错误: 未找到转录文件")
        return False

    log(f"转录文件: {os.path.basename(transcript)}")
    thinking, text = extract_last_response(transcript)

    print("\n" + "=" * 60)
    print("提取结果:")
    print("=" * 60)
    if thinking:
        print(f"\n[Thinking] ({len(thinking)} 字符):")
        print(thinking[:1000])
    if text:
        print(f"\n[Response] ({len(text)} 字符):")
        print(text[:1000])
    if not thinking and not text:
        print("\n>>> 未提取到内容")
    print("=" * 60)
    return True


# ======================== 入口 ========================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VS Bridge — 克劳德 ↔ 小龙虾桥接")
    parser.add_argument("--send", type=str, default=None,
                        help="发送文字到 VS Code Claude（自动粘贴+发送）")
    parser.add_argument("--status", action="store_true",
                        help="快速查看当前状态")
    parser.add_argument("--ocr-test", action="store_true",
                        help="转录文件读取测试：提取最后 Claude 回复")
    parser.add_argument("--paste", type=str, default=None,
                        help="仅复制文字到剪贴板（不点击发送）")
    args = parser.parse_args()

    if args.send:
        # 写入命令文件，由主循环统一处理（避免并发冲突）
        with open(COMMAND_FILE, "w", encoding="utf-8") as f:
            f.write(args.send)
        print(f"已加入命令队列: {args.send[:60]}...")
        print("主循环将在2秒内自动处理（发送→监控→转发微信）")
    elif args.paste:
        import pyperclip
        pyperclip.copy(args.paste)
        print(f"已复制到剪贴板: {args.paste}")
    elif args.status:
        quick_status()
    elif args.ocr_test:
        ocr_test()
    else:
        # 默认：主监控循环
        main_loop()
