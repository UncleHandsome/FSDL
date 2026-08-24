import os
import re
import json
import time
import random
import logging
import argparse
import shutil
import difflib
from pathlib import Path
from openai import OpenAI
from playwright.sync_api import sync_playwright

# ============================================================
# 日誌設定 (支援動態檔名)
# ============================================================
def setup_logger(log_file):
    logger = logging.getLogger("auto_tuner")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    
    # ⚡ [優化 1] 加上 mode='w'，讓腳本每次啟動時清空並覆寫 log，而不是無限接在後面 (預設為 'a')
    fh = logging.FileHandler(log_file, mode='w', encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

# ============================================================
# 多金鑰輪換池管理器 (API Key Pool)
# ============================================================
class ApiKeyPool:
    """多 API Key 負載、壞 Key 自動剔除與全滅狀態管理器"""
    def __init__(self, keys):
        self.all_keys = [k.strip() for k in keys if k and k.strip()]
        self.active_keys = list(self.all_keys)
        self.current_idx = 0

    def next_key_for_request(self, client):
        """★ 每一個 Request 輪流切換至下一把 Key"""
        if not self.active_keys:
            return ""
        self.current_idx = (self.current_idx + 1) % len(self.active_keys)
        new_key = self.active_keys[self.current_idx]
        self._apply_key_to_client(client, new_key)
        return new_key

    def get_current_key(self):
        if not self.active_keys:
            return ""
        return self.active_keys[self.current_idx % len(self.active_keys)]

    def rotate(self):
        """在所有有效金鑰之間輪流切換"""
        if not self.active_keys:
            return ""
        self.current_idx = (self.current_idx + 1) % len(self.active_keys)
        return self.active_keys[self.current_idx]

    def _apply_key_to_client(self, client, key):
        """深層穿透更新 OpenAI Client 實例的 API Key 與授權 Header"""
        if hasattr(client, "api_key"):
            client.api_key = key
        # 同步更新自訂 Header 與 httpx 內部 Header（避免大小寫重複設置導致 Cloudflare 400 Bad Request）
        if hasattr(client, "_custom_headers") and isinstance(client._custom_headers, dict):
            client._custom_headers["Authorization"] = f"Bearer {key}"
            client._custom_headers.pop("authorization", None)
        # 兼容最新 OpenAI Python SDK 內部 Client 配置
        if hasattr(client, "_client"):
            try:
                if hasattr(client._client, "headers"):
                    client._client.headers["Authorization"] = f"Bearer {key}"
            except Exception:
                pass
        if hasattr(client, "_auth") and hasattr(client._auth, "token"):
            try:
                client._auth.token = key
            except Exception:
                pass

    def rotate_client(self, client, logger, reason="觸發限制/異常"):
        """切換下一把金鑰並同步更新 OpenAI Client"""
        if not self.active_keys:
            return ""
        new_key = self.rotate()
        self._apply_key_to_client(client, new_key)
        logger.warning(
            f"    🔑 [金鑰輪換] {reason}，已自動切換至可用 Key ({self.mask_key(new_key)})"
        )
        return new_key

    def mark_current_dead(self, client, logger, reason="欠費/失效"):
        """★ 將當前 Key 永久剔除出活躍清單，並切換至下一把可用 Key（若全部陣亡回傳 True）"""
        if not self.active_keys:
            return True

        dead_key = self.get_current_key()
        if dead_key in self.active_keys:
            self.active_keys.remove(dead_key)

        logger.error(
            f"    💀 [金鑰陣亡] Key ({self.mask_key(dead_key)}) 因「{reason}」已永久剔除！"
            f"剩餘可用金鑰數: {len(self.active_keys)}/{len(self.all_keys)}"
        )

        if not self.active_keys:
            logger.critical("    🚫 [警報] 金鑰池中所有 API Key 皆已全數耗盡或失效！")
            return True

        self.current_idx = self.current_idx % len(self.active_keys)
        new_key = self.active_keys[self.current_idx]
        self._apply_key_to_client(client, new_key)
        logger.info(f"    ✨ 已無縫接軌切換至下一把正常 Key ({self.mask_key(new_key)})")
        return False

    def is_all_dead(self):
        """判斷是否全部 Key 皆已失效"""
        return len(self.active_keys) == 0

    def has_multiple(self):
        return len(self.active_keys) > 1

    def mask_key(self, key=None):
        k = key or self.get_current_key()
        if not k or len(k) <= 10:
            return "***"
        return f"{k[:6]}...{k[-4:]}"

    def __len__(self):
        return len(self.active_keys)


_LAST_API_CALL_TIME = 0.0


def load_api_keys(
    key_file=None,
    api_key_str=None,
    is_opencode=False,
    is_openrouter=False,
    is_free_glm=False,
    is_gemini=False,
    is_nvidia=False
):
    """讀取多 API Key 並構建 ApiKeyPool（支援多行、註解、逗號分隔）"""
    keys = []

    def _parse_keys(raw: str):
        res = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            for piece in re.split(r"[,;]+", line):
                piece = piece.strip().strip("'\"")
                if piece and not piece.startswith("#") and piece not in res:
                    res.append(piece)
        return res

    if api_key_str and api_key_str.strip():
        keys.extend(_parse_keys(api_key_str))

    # 本地金鑰檔案查找映射 (優先讀取檔案中的多把金鑰)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_map = {
        "gemini": ["gemini_key.txt", "google_key.txt", "gemini_api_key.txt", "api_key.txt"],
        "nvidia": ["nvidia_key.txt", "nvidia_api_key.txt", "nv_key.txt", "api_key.txt"],
        "openrouter": ["openrouter_key.txt", "openrouter_api_key.txt", "openrouter.txt", "or_key.txt", "glm_key.txt", "api_key.txt"],
        "opencode": ["opencode_key.txt", "opencode_api_key.txt", "api_key.txt"],
        "deepseek": ["api_key.txt", "deepseek_key.txt", "deepseek_api_key.txt", "key.txt"],
    }
    category = "gemini" if is_gemini else ("nvidia" if is_nvidia else ("openrouter" if (is_openrouter or is_free_glm) else ("opencode" if is_opencode else "deepseek")))
    candidate_filenames = file_map[category]

    cwd_dir = os.getcwd()
    candidate_paths = [key_file] if key_file else []
    for fname in candidate_filenames:
        candidate_paths.append(os.path.join(cwd_dir, fname))
        if base_dir != cwd_dir:
            candidate_paths.append(os.path.join(base_dir, fname))

    if not keys:
        for kf in candidate_paths:
            if kf and os.path.exists(kf):
                try:
                    with open(kf, "r", encoding="utf-8") as f:
                        parsed = _parse_keys(f.read())
                    if parsed:
                        keys.extend(parsed)
                        break
                except Exception:
                    pass

    # 環境變數查找映射 (備用)
    if not keys:
        if is_gemini:
            env_candidates = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"]
        elif is_nvidia:
            env_candidates = ["NVIDIA_API_KEY", "NV_API_KEY", "OPENAI_API_KEY"]
        elif is_free_glm or is_openrouter:
            env_candidates = ["OPENROUTER_API_KEY", "GLM_API_KEY", "OPENAI_API_KEY"]
        elif is_opencode:
            env_candidates = ["OPENCODE_API_KEY", "OPENAI_API_KEY"]
        else:
            env_candidates = ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]

        for var in env_candidates:
            val = os.environ.get(var)
            if val and val.strip():
                parsed = _parse_keys(val)
                if parsed:
                    keys.extend(parsed)
                    break

    if not keys:
        print("❌ 找不到 API 金鑰，請建立對應金鑰檔案或設定環境變數！")
        return None

    return ApiKeyPool(keys)

def extract_json_from_text(text):
    if not text or not isinstance(text, str):
        return None
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if not text:
        return None

    def _parse_candidate(raw_cand):
        if not raw_cand:
            return None
        cand = raw_cand.strip()
        # 1. 優先嘗試非嚴格解析 (容許字串內有未跳脫的換行符與控制字元)
        try:
            return json.loads(cand, strict=False)
        except Exception:
            pass
        # 2. 自動修復常見的尾隨逗號 (Trailing Commas)
        try:
            cleaned = re.sub(r',\s*([\]}])', r'\1', cand)
            return json.loads(cleaned, strict=False)
        except Exception:
            pass
        return None

    # 優先從 Markdown 代碼塊中提取
    match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', text, re.IGNORECASE)
    if match:
        res = _parse_candidate(match.group(1))
        if res is not None:
            return res

    # 其次提取最外層大括號 {}
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        res = _parse_candidate(text[first_brace : last_brace + 1])
        if res is not None:
            return res

    # 最後提取中括號陣列 []
    first_bracket = text.find('[')
    last_bracket = text.rfind(']')
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        res = _parse_candidate(text[first_bracket : last_bracket + 1])
        if res is not None:
            return res

    return None

def extract_js_from_html(html_content):
    scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', html_content, re.DOTALL)
    if scripts:
        clean_js = "\n\n".join(s.strip() for s in scripts)
        return clean_js.replace('\r\n', '\n')
    return html_content.replace('\r\n', '\n')

def clean_code_block(text):
    """去除 AI 幻覺夾帶的 Markdown 代碼塊標籤"""
    if not text: return text
    text = re.sub(r'^```[a-zA-Z]*\r?\n', '', text)
    text = re.sub(r'\r?\n```$', '', text)
    return text.strip('\n')

# ============================================================
# 瀏覽器自動化：執行 Dry Run 收集數據
# ============================================================
def run_browser_simulations(html_path, num_runs, logger, is_exam=False):
    results = []
    abs_path = os.path.abspath(html_path)
    file_url = Path(abs_path).as_uri()
    
    mode_text = "【大考模式 - executeSuite】" if is_exam else f"【一般取樣模式 - {num_runs} 次】"
    logger.info(f"🌐 啟動無頭瀏覽器，準備執行 Dry Run {mode_text}...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--enable-webgl",
                "--ignore-gpu-blocklist",
                "--use-gl=angle",
                "--use-angle=swiftshader-webgl"  # 確保無頭模式下 WebGL / Three.js 100% 正常運作
            ]
        ) 
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        
        # 屏蔽 alert 彈窗，避免阻塞 Chrome 事件循環
        page.add_init_script("window.alert = () => {};")
        
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("dialog", lambda dialog: dialog.accept())
        
        try:
            page.goto(file_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            
            if page_errors:
                logger.error(f"❌ 頁面載入失敗，發現 JavaScript 語法錯誤:\n   {page_errors[0]}")
                browser.close()
                return ("JS_ERROR", page_errors[0])

            valid_results = []
            passed_core_count = 0
            seen_hashes = set()

            def clean_and_hash(res):
                if "meta" in res: del res["meta"]
                return json.dumps(res, sort_keys=True)

            dry_run_script = """
            (() => {
                if (typeof DryRunTool !== 'undefined' && DryRunTool.executeSingle) {
                    return DryRunTool.executeSingle(6, true);
                } else if (window.DryRunTool && window.DryRunTool.executeSingle) {
                    return window.DryRunTool.executeSingle(6, true);
                }
                return null;
            })()
            """

            if is_exam:
                logger.info("  ▶ [階段 1] 執行全格局覆蓋測試 (executeSuite)...")
                exam_script = """
                (() => {
                    if (typeof DryRunTool !== 'undefined' && DryRunTool.executeSuite) {
                        return DryRunTool.executeSuite(6);
                    } else if (window.DryRunTool && window.DryRunTool.executeSuite) {
                        return window.DryRunTool.executeSuite(6);
                    }
                    return null;
                })()
                """
                raw_results = page.evaluate(exam_script)
                if raw_results:
                    if not isinstance(raw_results, list): raw_results = [raw_results]
                    for res in raw_results:
                        preset_name = res.get("presetName", "")
                        is_fixed = preset_name and not preset_name.startswith("random")
                        has_warnings = len(res.get("sanityWarnings", [])) > 0
                        expected = res.get("expectedRating", "動態判定")
                        actual = res.get("scoringAndVerdict", {}).get("verdictRating", "") or res.get("verdict", {}).get("rating", "") or res.get("verdict", "")
                        rating_matches = (expected == "動態判定") or any(exp in actual for exp in expected.split("/"))

                        if is_fixed and not has_warnings and rating_matches:
                            passed_core_count += 1
                            continue

                        res_hash = clean_and_hash(res)
                        if res_hash not in seen_hashes:
                            seen_hashes.add(res_hash)
                            valid_results.append(res)
            else:
                all_btns = page.evaluate("Array.from(document.querySelectorAll('.preset-btn')).map(b => b.dataset.p)")
                fixed_presets = [p for p in all_btns if p != 'random'] if all_btns else ['perfect']

                logger.info(f"  ▶ [階段 1] 執行 {len(fixed_presets)} 項固定格局防迴歸測試...")
                for i, preset in enumerate(fixed_presets):
                    if i > 0 and i % 5 == 0:
                        page_errors.clear()
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(1000)
                        if page_errors: return ("JS_ERROR", page_errors[0])

                    page_errors.clear()
                    page.evaluate(f"document.querySelector(\".preset-btn[data-p='{preset}']\")?.click()")
                    page.wait_for_timeout(1500)
                    if page_errors: return ("JS_ERROR", page_errors[0])

                    try:
                        # 設定 15 秒超時保護，兼顧 WebGL 渲染負載並防止死迴圈
                        res = page.evaluate(f"""
                            Promise.race([
                                Promise.resolve().then(() => {dry_run_script}),
                                new Promise((_, reject) => setTimeout(() => reject(new Error('Dry Run 超時 (可能存在死迴圈)')), 15000))
                            ])
                        """)
                    except Exception as eval_err:
                        logger.error(f"  ❌ 執行 Dry Run 評估失敗或超時: {eval_err}")
                        res = None
                    if not res: continue

                    has_warnings = len(res.get("sanityWarnings", [])) > 0
                    expected = res.get("expectedRating", "動態判定")
                    actual = res.get("scoringAndVerdict", {}).get("verdictRating", "") or res.get("verdict", {}).get("rating", "") or res.get("verdict", "")
                    rating_matches = (expected == "動態判定") or any(exp in actual for exp in expected.split("/"))

                    if not has_warnings and rating_matches:
                        passed_core_count += 1
                        continue

                    res_hash = clean_and_hash(res)
                    if res_hash not in seen_hashes:
                        seen_hashes.add(res_hash)
                        valid_results.append(res)

            # 【核心機制】計算不足的扣打，瘋狂跑 Random 補滿！
            shortfall = num_runs - len(valid_results)
            if shortfall > 0:
                logger.info(f"  ▶ [階段 2] 核心測試過濾完畢 (隱藏 {passed_core_count} 筆完美數據)，準備執行 {shortfall} 次隨機測試補滿額度...")
                attempts = 0
                while len(valid_results) < num_runs and attempts < shortfall * 4: # 設定最大嘗試次數防無限迴圈
                    attempts += 1
                    if attempts % 5 == 0:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(1000)
                        if page_errors: return ("JS_ERROR", page_errors[0])

                    page.evaluate("document.querySelector(\".preset-btn[data-p='random']\")?.click()")
                    page.wait_for_timeout(1500)
                    if page_errors: return ("JS_ERROR", page_errors[0])

                    res = page.evaluate(dry_run_script)
                    if not res: continue

                    res_hash = clean_and_hash(res)
                    # 只有真正產生出不同地形與參數特徵的 random，才會被收錄
                    if res_hash not in seen_hashes:
                        seen_hashes.add(res_hash)
                        valid_results.append(res)
                        logger.info(f"    - 獲取有效隨機探索樣本 ({len(valid_results)}/{num_runs})")

            # 亂序交錯，防止 AI 產生閱讀偏誤
            random.shuffle(valid_results)
            
            # 使用 Tuple 回傳多個資訊給主程序
            return ("SUCCESS", valid_results, passed_core_count)
            
        except Exception as e:
            logger.error(f"❌ 瀏覽器運行過程發生例外: {e}")
            browser.close()
            return None
            
        browser.close()
        
    return results
    
# ============================================================
# 共用 API 異常處理與重試退避機制 (同步自 sutra.py)
# ============================================================
def handle_api_exception(
    e,
    client,
    model,
    logger,
    retry,
    max_retries,
    context_desc=""
):
    """
    統一處理 API 請求異常、金鑰剔除/輪換與階梯退避時間計算
    回傳: (should_terminate: bool, backoff_seconds: float)
    """
    err_msg = str(e)
    pool = getattr(client, "key_pool", None)

    # 1. 模型不可用或下線
    model_fatal_keywords = [
        "unavailable for free", "use this slug instead", "model_not_found",
        "does not exist", "no allowed providers are available"
    ]
    if any(kw in err_msg.lower() for kw in model_fatal_keywords):
        logger.critical(f"❌ [模型不可用] 模型 '{model}' 無法使用 ({err_msg})！請使用 --model 指定其他可用模型。")
        return True, 0.0

    # 2. 模型協議不匹配 (例如 OpenCode Zen 需要走 Responses API)
    if "modelerror" in err_msg.lower() or "not supported" in err_msg.lower():
        logger.error(f"❌ [模型協議/端點錯誤] 模型 '{model}' 無法在此端點運行 ({err_msg})！")
        return True, 0.0

    # 3. 帳號失效、欠費或每日配額耗盡
    fatal_keywords = [
        "insufficient balance", "creditserror", "authenticationerror",
        "invalid_api_key", "402", "payment required",
        "exceeded your current quota", "resource_exhausted", "quota exceeded",
        "generaterequestsperday", "perday"
    ]
    is_fatal = any(kw in err_msg.lower() for kw in fatal_keywords) or ("401" in err_msg and "model" not in err_msg.lower())

    if is_fatal:
        if pool:
            all_dead = pool.mark_current_dead(client, logger, reason=f"額度耗盡/失效 ({err_msg[:40]})")
            if all_dead:
                logger.error("❌ 所有 API 金鑰皆已失效或配額耗盡！流水線立即安全中止。")
                return True, 0.0
            return False, 1.0  # 切換新 Key 後立即重試
        else:
            logger.error(f"❌ API 金鑰無效或帳號餘額不足 ({err_msg})！流水線立即中止。")
            return True, 0.0

    # 3. 嘗試從錯誤訊息中提取官方建議等待秒數 (支援 OpenRouter / Google)
    retry_delay = 0.0
    m_retry = re.search(r"(?:retry_after_seconds['\"]?:\s*|retry in\s+|retryDelay['\"]?:\s*['\"]?)(\d+(?:\.\d+)?)", err_msg, re.IGNORECASE)
    if m_retry:
        try:
            retry_delay = float(m_retry.group(1))
        except Exception:
            pass

    # 4. 限流（429 / RESOURCE_EXHAUSTED / 配額限制）或網路異常：輪換 Key 並智慧退避
    if pool is not None and pool.has_multiple():
        is_rate_limit = any(kw in err_msg.lower() for kw in ["429", "rate", "quota", "resource_exhausted"])
        reason_desc = "觸發頻率限制 (429/RPM)" if is_rate_limit else f"API 請求異常 ({err_msg[:45]})"
        pool.rotate_client(client, logger, reason=reason_desc)
        if retry_delay > 0:
            backoff_time = max(retry_delay + 2.0, 6.0)
        else:
            backoff_time = min(30.0, (retry + 1) * 6.0)
    else:
        if retry_delay > 0:
            backoff_time = max(retry_delay + 3.0, 8.0)
        else:
            is_free_or_rate_limit = (
                ":free" in model.lower()
                or "openrouter" in str(getattr(client, "base_url", "")).lower()
                or "googleapis" in str(getattr(client, "base_url", "")).lower()
                or "nvidia" in str(getattr(client, "base_url", "")).lower()
                or "429" in err_msg
                or "rate" in err_msg.lower()
                or "quota" in err_msg.lower()
            )
            backoff_time = min(120.0, (retry + 1) * 15.0) if is_free_or_rate_limit else min(30.0, (retry + 1) * 5.0)

    desc_str = f"（{context_desc}）" if context_desc else ""
    logger.error(
        f"    ❌ API 呼叫失敗 ({e}){desc_str}，等待 {backoff_time:.1f} 秒後進行第 {retry + 1}/{max_retries} 次重試..."
    )
    return False, backoff_time

# ============================================================
# 多輪對話 AI 診斷 (同步 sutra.py 時間間隔與 API 規範)
# ============================================================
def get_ai_correction_multiturn(client, model, conversation_history, logger):
    global _LAST_API_CALL_TIME
    logger.info(f"🧠 正在請求 AI 模型 ({model}) 進行診斷 (對話歷史深度: {len(conversation_history)})...")

    pool = getattr(client, "key_pool", None)
    if pool and pool.is_all_dead():
        logger.error("🛑 金鑰池中所有 API Key 皆已失效，無法發送請求！")
        return None, "ALL_KEYS_DEAD", 0

    max_retries = max(len(pool) * 2, 6) if (pool and pool.has_multiple()) else 3

    is_third_party_or_free = (
        ":free" in model.lower()
        or "glm" in model.lower()
        or "gemini" in model.lower()
        or "dots" in model.lower()
        or "openrouter" in str(getattr(client, "base_url", "")).lower()
        or "googleapis" in str(getattr(client, "base_url", "")).lower()
    )

    m_lower = model.lower()
    if "dots" in m_lower:
        max_tokens_val = 200000
    elif "nvidia" in str(getattr(client, "base_url", "")).lower():
        max_tokens_val = 16384
    elif "glm-5.2" in m_lower or "glm" in m_lower:
        max_tokens_val = 150000
    elif "muse" in m_lower or "spark" in m_lower:
        max_tokens_val = 256000
    elif "ox" in m_lower or "preview" in m_lower or "alpha" in m_lower:
        max_tokens_val = 131072
    elif "gemini" in m_lower or "googleapis" in str(getattr(client, "base_url", "")).lower():
        max_tokens_val = 65536
    elif is_third_party_or_free:
        max_tokens_val = 65536
    else:
        max_tokens_val = 384000

    for attempt in range(max_retries):
        # 1. 時間間隔控制 (OpenRouter / NVIDIA / Gemini 每 10 秒，其餘每 2 秒)
        url_and_model = f"{client.base_url} {model}".lower()
        target_interval = 10.0 if any(kw in url_and_model for kw in ["openrouter", "nvidia", "gemini", "googleapis"]) else 2.0

        now = time.time()
        elapsed = now - _LAST_API_CALL_TIME
        if elapsed < target_interval:
            wait_seconds = target_interval - elapsed
            logger.info(f"  ⏳ [頻率管控] 距離上次請求間隔保護中，等待 {wait_seconds:.1f} 秒...")
            time.sleep(wait_seconds)
        _LAST_API_CALL_TIME = time.time()

        # 2. 金鑰輪換至下一把
        if pool and len(pool.active_keys) > 0:
            pool.next_key_for_request(client)

        create_kwargs = {
            "model": model,
            "messages": conversation_history,
            "temperature": 1.0,
            "max_tokens": max_tokens_val,
            "reasoning_effort": "high",
            "extra_body": {
                "reasoning": {
                    "effort": "max"
                }
            },
        }

        is_opencode_responses = "opencode.ai/zen" in str(getattr(client, "base_url", "")).lower() and (
            "muse" in model.lower() or "spark" in model.lower()
        )

        try:
            if is_opencode_responses:
                # OpenCode Zen / Go 專屬 Responses API 協議轉發 (開啟高強度思考)
                try:
                    resp_data = client.post(
                        "responses",
                        cast_to=object,
                        body={
                            "model": model,
                            "input": conversation_history,
                            "temperature": 1.0,
                            "max_output_tokens": min(max_tokens_val, 65536),
                            "reasoning": {"effort": "max"},
                            "reasoning_effort": "max",
                        }
                    )
                    raw_response = ""
                    usage = None
                    reasoning_text = ""
                    if isinstance(resp_data, dict):
                        raw_response = resp_data.get("output_text", "")
                        usage = resp_data.get("usage")
                        if "reasoning_content" in resp_data:
                            reasoning_text = resp_data.get("reasoning_content", "")
                        if not raw_response and "output" in resp_data:
                            for item in resp_data.get("output", []):
                                if isinstance(item, dict):
                                    item_type = item.get("type", "")
                                    if item_type in ["reasoning", "thought", "thinking"]:
                                        for c in item.get("content", []):
                                            if isinstance(c, dict) and c.get("text"):
                                                reasoning_text += c.get("text")
                                    elif "content" in item:
                                        for c in item.get("content", []):
                                            if isinstance(c, dict) and c.get("text"):
                                                raw_response += c.get("text")
                    elif hasattr(resp_data, "output_text"):
                        raw_response = getattr(resp_data, "output_text", "")
                        usage = getattr(resp_data, "usage", None)

                    if reasoning_text and "<think>" not in raw_response:
                        raw_response = f"<think>\n{reasoning_text.strip()}\n</think>\n\n{raw_response}"
                except Exception:
                    # 容錯回退至標準 Chat Completions 端點
                    response = client.chat.completions.create(**create_kwargs)
                    usage = getattr(response, "usage", None)
                    msg_obj = response.choices[0].message
                    raw_response = msg_obj.content or ""
                    r_content = getattr(msg_obj, "reasoning_content", None) or getattr(msg_obj, "reasoning", None) or getattr(msg_obj, "thought", None)
                    if r_content and "<think>" not in raw_response:
                        raw_response = f"<think>\n{r_content.strip()}\n</think>\n\n{raw_response}"
            else:
                try:
                    response = client.chat.completions.create(**create_kwargs)
                except Exception as api_err:
                    err_str = str(api_err).lower()
                    if "max_tokens" in err_str or "maximum allowed" in err_str:
                        create_kwargs["max_tokens"] = 8192
                    if "reasoning_effort" in err_str or "extra" in err_str:
                        create_kwargs.pop("reasoning_effort", None)
                    if "extra_body" in create_kwargs:
                        create_kwargs.pop("extra_body", None)
                    response = client.chat.completions.create(**create_kwargs)

                usage = getattr(response, "usage", None)
                msg_obj = response.choices[0].message
                raw_response = msg_obj.content or ""
                r_content = getattr(msg_obj, "reasoning_content", None) or getattr(msg_obj, "reasoning", None) or getattr(msg_obj, "thought", None)
                if r_content and "<think>" not in raw_response:
                    raw_response = f"<think>\n{r_content.strip()}\n</think>\n\n{raw_response}"

            _LAST_API_CALL_TIME = time.time()

            if usage:
                def _get_val(obj, *keys, default=0):
                    if obj is None:
                        return default
                    for k in keys:
                        if isinstance(obj, dict) and k in obj and obj[k] is not None:
                            return obj[k]
                        elif hasattr(obj, k) and getattr(obj, k) is not None:
                            return getattr(obj, k)
                    return default

                total_prompt = _get_val(usage, "prompt_tokens", "input_tokens", default=0)
                completion_tokens = _get_val(usage, "completion_tokens", "output_tokens", default=0)
                
                # 提取快取命中
                hit_tokens = _get_val(usage, "prompt_cache_hit_tokens", "cache_read_input_tokens", default=0)
                prompt_details = _get_val(usage, "prompt_tokens_details", "input_token_details", default=None)
                if prompt_details and hit_tokens == 0:
                    hit_tokens = _get_val(prompt_details, "cached_tokens", "cache_read", default=0)
                
                miss_tokens = _get_val(usage, "prompt_cache_miss_tokens", "cache_creation_input_tokens", default=0)
                if miss_tokens == 0 and total_prompt > hit_tokens:
                    miss_tokens = total_prompt - hit_tokens

                # 提取思考 Token
                comp_details = _get_val(usage, "completion_tokens_details", "output_token_details", default=None)
                reasoning_tokens = _get_val(comp_details, "reasoning_tokens", default=0)
                reasoning_str = f" (含思考: {reasoning_tokens})" if reasoning_tokens > 0 else ""
                
                hit_rate = (hit_tokens / total_prompt * 100) if total_prompt > 0 else 0
                total_tokens = total_prompt + completion_tokens
                logger.info(f"  ⚡ Token 消耗: 輸入 {total_prompt} (快取命中: {hit_tokens} / {hit_rate:.1f}%, 未命中: {miss_tokens}) | 輸出 {completion_tokens}{reasoning_str} | 總計 {total_tokens}")

            logger.info(f"💬 [AI 原始回覆內容]:\n{'='*50}\n{raw_response}\n{'='*50}")

            extracted_json = extract_json_from_text(raw_response)
            if not extracted_json:
                logger.warning(f"⚠️ 警告：無法從 AI 原始回覆中解析出有效的 JSON 結構！(嘗試 {attempt+1}/{max_retries})")
                if not raw_response.strip():
                    logger.error("🛑 偵測到 AI 回傳完全空白！(可能是單一檔案過大超出 128K Token 極限，或 API 崩潰)")
                    if len(conversation_history) <= 2:
                        logger.error("💀 致命錯誤：首輪代碼快照即突破 API 極限！強制終止腳本！")
                        os._exit(1)
                    return {"status": "CONTEXT_LIMIT", "reason": "API 回傳完全空白，觸發緊急重置"}, "", 0

                time.sleep(2)
                continue

            history_text = re.sub(r"<think>[\s\S]*?</think>", "", raw_response, flags=re.IGNORECASE).strip()
            prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
            return extracted_json, history_text, prompt_tokens

        except KeyboardInterrupt:
            logger.warning("\n🛑 使用者中斷了 AI 請求流程 (Ctrl+C)")
            raise
        except Exception as e:
            _LAST_API_CALL_TIME = time.time()
            error_msg = str(e).lower()
            if "context_length_exceeded" in error_msg or "context length" in error_msg or "too large" in error_msg:
                logger.error("🚨 偵測到 Token 歷史爆量！準備緊急觸發硬重置...")
                return {"status": "CONTEXT_LIMIT", "reason": "歷史 Token 爆量，觸發緊急重置"}, error_msg, 0

            should_term, backoff = handle_api_exception(
                e=e,
                client=client,
                model=model,
                logger=logger,
                retry=attempt,
                max_retries=max_retries,
                context_desc=f"第 {attempt+1}/{max_retries} 次診斷呼叫"
            )
            if should_term:
                return None, str(e), 0
            time.sleep(backoff)

    return None, "", 0

# ============================================================
# 代碼修改套用與回滾 (原子寫入防損毀)
# ============================================================
def apply_code_modifications(html_path, changes, logger):
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            original_content = f.read()
            
        content = original_content.replace('\r\n', '\n')
        applied_count = 0
        
        for idx, change in enumerate(changes):
            # ⚡ [容錯優化] 消除 AI 生成區塊首尾常附帶的隱形空行干擾
            search_text = clean_code_block(change.get("search", "")).replace('\r\n', '\n').strip('\n')
            replace_text = clean_code_block(change.get("replace", "")).replace('\r\n', '\n').strip('\n')
            
            if not search_text:
                continue
            
            logger.info(f"\n  📝 [修改片段 #{idx+1} 代碼比對]")
            logger.info("  --- [尋找 (Search)] ---")
            for line in search_text.split('\n'):
                logger.info(f"    - {line}")
            logger.info("  +++ [替換 (Replace)] +++")
            for line in replace_text.split('\n'):
                logger.info(f"    + {line}")
            logger.info("  --------------------------------------")

            count = content.count(search_text)
            if count == 1:
                content = content.replace(search_text, replace_text, 1)
                applied_count += 1
                logger.info(f"  ✅ 成功套用修改片段 #{idx+1} (100% 精確匹配)")
                continue
            elif count > 1:
                logger.warning(f"  ⚠️ 片段 #{idx+1} 在檔案中出現 {count} 次，具有歧義性！嘗試區域匹配...")

            lines = content.split('\n')
            search_lines = search_text.split('\n')
            search_len = len(search_lines)
            
            found_indices = []
            for i in range(len(lines) - search_len + 1):
                match = True
                for j in range(search_len):
                    if lines[i + j].rstrip() != search_lines[j].rstrip():
                        match = False
                        break
                if match:
                    found_indices.append(i)
            
            if len(found_indices) == 1:
                found_idx = found_indices[0]
                lines[found_idx : found_idx + search_len] = replace_text.split('\n')
                content = '\n'.join(lines)
                applied_count += 1
                logger.info(f"  ✅ [容錯模式] 成功套用修改片段 #{idx+1} (忽略行尾空白)")
                continue

            clean_search_code = [l.strip() for l in search_lines if l.strip() and not l.strip().startswith('//')]
            if clean_search_code:
                code_len = len(clean_search_code)
                found_code_idx = -1
                match_count = 0
                for i in range(len(lines) - code_len + 1):
                    sub_chunk = [lines[i + k].strip() for k in range(code_len)]
                    if sub_chunk == clean_search_code:
                        match_count += 1
                        found_code_idx = i
                
                if match_count == 1:
                    lines[found_code_idx : found_code_idx + code_len] = replace_text.split('\n')
                    content = '\n'.join(lines)
                    applied_count += 1
                    logger.info(f"  ✅ [智慧容錯模式] 成功套用修改片段 #{idx+1} (忽略註解與空白差異)")
                    continue

            # 策略 4: 超強模糊滑動視窗匹配 (容許數值微調與個別字元差異)
            best_ratio = 0.0
            best_idx = -1
            best_w_len = search_len

            search_block = '\n'.join(search_lines)
            for w_len in range(max(1, search_len - 2), min(len(lines), search_len + 3)):
                for i in range(len(lines) - w_len + 1):
                    window_str = '\n'.join(lines[i : i + w_len])
                    ratio = difflib.SequenceMatcher(None, window_str, search_block).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_idx = i
                        best_w_len = w_len

            # ⚡ [優化 2] 提高模糊匹配的安全門檻 (從 0.75 提高至 0.92)
            # 在代碼替換中，低於 92% 的相似度極高機率會導致覆蓋錯行或丟失大段邏輯，必須嚴格攔截！
            if best_ratio >= 0.92 and best_idx != -1:
                lines[best_idx : best_idx + best_w_len] = replace_text.split('\n')
                content = '\n'.join(lines)
                applied_count += 1
                logger.info(f"  ✅ [超強模糊匹配模式] 成功套用修改片段 #{idx+1} (相似度: {best_ratio*100:.1f}%)")
                continue
            elif best_idx != -1 and best_ratio >= 0.70:
                # 攔截並警告：雖然找到很像的，但差異太大，拒絕套用
                logger.warning(f"  ⚠️ 拒絕模糊匹配！找到最相似的片段僅 {best_ratio*100:.1f}% 相似度，為防代碼損毀，強制退回重寫！")

            logger.warning(f"  ❌ 找不到匹配的字串，無法套用修改片段 #{idx+1}！")
        
        # ⚡ [防呆與原子寫入優化] 必須「所有片段」都匹配成功才准許存檔，否則整批退回要求 AI 重寫
        if applied_count == len(changes) and applied_count > 0:
            # 備份原始檔
            backup_path = html_path + ".bak"
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(original_content)
                
            # 原子寫入：先寫入 .tmp 再覆蓋
            tmp_path = html_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp_path, html_path)
            
            logger.info(f"💾 檔案已儲存更新: {html_path} (舊檔備份於 {backup_path})")
            return True
        elif applied_count > 0:
            logger.error(f"❌ [部分套用失敗] 只有 {applied_count}/{len(changes)} 個片段匹配成功。為防止代碼損毀，已撤銷整批修改！")
            return False
            
        return False
    except Exception as e:
        logger.error(f"❌ 修改檔案失敗: {e}")
        return False

def rollback_file(html_path, logger):
    backup_path = html_path + ".bak"
    if os.path.exists(backup_path):
        shutil.copyfile(backup_path, html_path)
        logger.warning(f"🔄 已從備份檔 {backup_path} 自動回滾復原 {html_path}")

# ============================================================
# 全域統一 System Prompt (確保靜態與動態模式 System Prompt 100% Cache Hit)
# ============================================================
UNIFIED_SYSTEM_PROMPT = r"""你是一個頂尖的 3D 風水模擬器核心開發者、WebGL/Three.js 幾何專家與湧現計算流體物理學家。
我們的對話將維持連續歷史紀錄。你將會收到兩種類型的診斷請求：
1. 【動態 Dry Run 診斷】：分析傳入的多輪測試數據 JSON。
2. 【靜態代碼審查】：分析傳入的最新 JS 代碼快照。

🔥🔥🔥 【湧現物理與風水本體論（嚴禁任何字串與屬性硬編碼）】 🔥🔥🔥
風水物理引擎嚴禁依賴任何輸入參數標籤（如嚴禁在動力學中寫 `if (state.ms === 'sunken')`、`if (state.terrain === 'fanpo')` 或 `if (state.w === 'tfork')`）。
所有風水吉凶必須 100% 由「空間拓撲幾何」、「CFD 向量流場」與「局部動力學微分特性」自然湧現！

1. 🚨【界水則止與水法本質（Boundary Layer Stagnation）】:
   - 《葬書》「氣乘風則散，界水則止」：水體是生氣流動的物理邊界。
   - 【物理機制】：慢速環抱水體（玉帶水）在陸水交界處應具備水陸邊界層阻尼，使前進的氣流自然減速沉降於明堂與太極暈前方；僅在水體剪切亂流過高（急流沖割、三叉亂流 $\|\nabla \times \vec{v}\| > 0.8$）時，才轉化為水煞。嚴禁將所有水體一律視為負壓黑洞！

2. 🚨【窩穴聚氣 vs 陷坑死水（Topological Curvature & Ventilation）】:
   - 傳統四大正穴（窩、鉗、乳、突）中，「窩穴」為開闊微凹地形，氣流在其中形成舒緩渦旋，屬大吉結穴。
   - 【真偽判定】：由地形局部幾何曲率（二階偏導 $\nabla^2 h$）與通風通量（Flux）客觀判定：
     * **吉（窩穴太極暈）**：盆地開闊（寬深比 $W/D > 4$），微凹聚氣，氣流通量 $\Phi > 0$ 且為層流沉降（$speedSq < 0.35$）。
     * **凶（陷煞死坑）**：深度過大且封閉（$W/D < 2$），粒子垂直陷落後無法逃逸，滯留時間過長且陰濕失衡（$yinYangBalance < -4$）。
   - 判定必須完全依賴微積分幾何與流體速度，嚴禁寫死名稱過濾！

3. 🚨【山龍與平洋龍全域統一驅動（Geopotential & Drift Unification）】:
   - 《撼龍經》「平洋一突值千金」、「高一寸為山，低一寸為水」。
   - 【物理機制】：粒子推力向量 $\vec{F}_{drive} = -\alpha \nabla h + \beta \vec{v}_{drift}$。
     * 在高山（山龍），地勢坡度梯度 $-\nabla h$ 主導粒子沿山脊向下奔馳。
     * 在平原（平洋龍，$\nabla h \approx 0$），由大氣宏觀背景微壓差與水流牽引向量 $\vec{v}_{drift}$ 主導推進。
   - 兩者共用同一套力學方程式，確保平洋地貌絕不發生流體死鎖（Zero-Activity Stall）。

4. 🚨【理氣全周天連續性與空亡線（Circular Continuous Li Qi）】:
   - 羅盤 $0^\circ \sim 360^\circ$ 為連續圓周流形。八宮（45°/宮）與二十四山（15°/山）判定嚴禁寫死離散 `if-else` 分支。
   - 坐向與方位計算一律使用三角諧波或模運算（`let deg = (rawDeg % 360 + 360) % 360`）。
   - 出卦與空亡線判定：利用距分界線之角距離 $d_{edge} = |(deg + 7.5) \bmod 15 - 7.5|$ 連續衰減，自然呈現線位穩定性。

🔥🔥🔥 【動態數據審查：四大物理死穴審計協議】 🔥🔥🔥
審查 Dry Run JSON 時，【絕對禁止】只看 `verdictRating` 就判定正常。只要數據命中以下任一死穴，【強制判定 MODIFIED】：
1. 🚨【凶煞偽聚氣 (False Gathering)】：局部亂流、反坡逆風或封閉死坑處，`gatherAcc > 500` 且 `yinYangBalance < -4`（層流條件 `isCalmFlow` 誤判滯留死水為結穴）。
2. 🚨【流體死鎖 (Zero-Activity Stall)】：`gatherAcc === 0 && scatterAcc === 0`（全域推力或平洋龍未正確給予背景場，粒子未進場）。
3. 🚨【猝死震盪 (Respawn Thrashing)】：`scatterAcc > 4000`（粒子出生點落入極端幾何斷崖或強煞區，剛生成立即死亡循環）。
4. 🚨【防呆容量超載 (Capacity Violation)】：明堂截斷或無護砂情況下，聚氣量超出該拓撲空間之幾何容積。

🔥🔥🔥 【圖學、幾何與數值穩定性鐵律】 🔥🔥🔥
1. **全無硬編碼相對尺度**：穴位周邊所有幾何、力場衰減一律基於局部相對坐標 $\Delta \vec{r} = \vec{x} - \vec{x}_{xue}$，嚴禁出現任何寫死的絕對坐標常量（如嚴禁寫 `p.z < -30`）。
2. **三維單向地面托舉與水體浮力**：陸地為單向幾何支撐 `y = Math.max(y, t.y + 0.8)`，水域垂向由浮力阻尼自然平衡，允許三維立體升降，嚴禁拍扁在單一平面。
3. **平滑過渡（Smoothstep / Gaussian Falloff）**：嚴禁階梯式硬切斷（Hard Cutoff），分母必須防禦除零（`Math.max(0.001, dist)`），嚴防 3D 破圖與 NaN 崩潰。
4. **單向數據流**：【3D地形/CFD網格】 $\rightarrow$ 【粒子動力學】 $\rightarrow$ 【Sensors物理採樣】 $\rightarrow$ 【Rules五訣評分】。評分引擎僅為客觀觀測者，嚴禁修改底層物理場。

🔥🔥🔥 【代碼修改與輸出鐵律 (CRITICAL PATCHING RULES)】 🔥🔥🔥
1. **【零註解原則 (Zero-Comment Invariance)】**：`replace` 區塊中**絕對嚴禁添加任何自創註解**（如禁止寫 `// 修正...`、`// 優化`）。代碼必須是 100% 純淨邏輯。
2. **【嚴格保留原始縮排與換行】**：`search` 與 `replace` 中的每一行，縮排與換行必須與目標檔案 100% 精確對齊。
3. **【一字不漏的 Search 區塊】**：`search` 必須提供 5~10 行完整上下文，嚴禁使用 `// ... (省略)`！
4. **【審計輸出決策】**：只要發現物理不合規或代碼存在邏輯漏洞，回傳 MODIFIED；若完全符合物理真實性，方可回傳 PERFECT。

【輸出格式】
若發現問題需修改：
{
  "status": "MODIFIED",
  "reason": "[病徵] 描述數據異常。\n[病因] 定位代碼邏輯漏洞（基於流體或幾何場論）。\n[解法] 說明具體數學與物理修正方式。",
  "changes": [
    {
      "search": "                // 原目標代碼完整上下文 (5~10行)",
      "replace": "                // 替換後的新代碼 (零註解、嚴格縮排)"
    }
  ]
}

若審計完全無問題：
{
  "status": "PERFECT",
  "reason": "已完成幾何與流體數據深度審計，各格局之聚散比率、水陸邊界層減速、CFD 向量與幾何邊界均完全符合湧現物理與風水本體論。"
}
"""

# ============================================================
# 每輪尾部強制重申的終極十全緊箍咒 (封死所有物理作弊、除零崩潰與變數未定義)
# ============================================================
CORE_RULES_REMINDER = r"""
⚡⚡⚡【全域物理湧現、去硬編碼與代碼修改十全天條（每輪強制重申・違者退回）】⚡⚡⚡
1. 【零標籤硬編碼】嚴禁在物理引擎中使用格局名稱字串判斷（如嚴禁 `state.ms === 'sunken'` 或 `state.w === 'tfork'`）！所有吉凶必須由「曲率張量 $\nabla^2 h$」、「流場剪切亂流 $\|\nabla \times \vec{v}\|$」與「水陸邊界層阻尼」客觀決定！

2. 【界水則止真物理】「氣乘風則散，界水則止」：緩慢環抱之水體在岸邊應自然形成邊界層減速（促成生氣沉降於太極暈）；僅在急流強剪切時才產生水煞，嚴禁將所有水體一律當成負壓排斥黑洞！

3. 【山龍平洋龍統一推力】推力由地勢梯度 $-\nabla h$ 與背景微壓差場 $\vec{v}_{drift}$ 自然合成，平洋龍（$\nabla h \approx 0$）依賴水流牽引與環境微風，嚴禁平洋地貌發生流體死鎖（gatherAcc/scatterAcc 皆為 0）！

4. 【相對坐標尺度不變性】穴周邊幾何與力場判定一律使用相對坐標 ($\Delta \vec{r} = \vec{x} - \vec{x}_{xue}$)，嚴禁寫死任何絕對坐標常量（如 `Z ≈ -30` 或絕對網格下標）！

5. 【三維立體自由度】陸地單向托舉（`y = Math.max(y, t.y + 0.8)`），水域由浮力自然平衡，嚴禁拍扁在固定高度二維薄片上！

6. 【理氣全周天連續性】方位與二十四山一律使用模運算與三角諧波連續計算（`let deg = (rawDeg % 360 + 360) % 360`），嚴禁離散多分支硬特判正北坎宮！

7. 【唯一狀態機收斂與結穴】粒子狀態 (`pState`) 100% 只能由動能閾值（`speedSq > 3.5` 散氣）、局部剪切亂流（`turbulence > 0.8` 紅煞）以及層流沉降條件（`speedSq < 0.35 && isCalmFlow` 吉氣 `pState = 2`）判定；開闊太極暈微凹處為唯一合法結穴點！

8. 【平滑過渡與除零防禦】向量與距離相除必加防禦分母 (`Math.max(0.001, dist)`)，力場與地形衰減一律使用高斯或 Smoothstep，嚴禁階梯式硬切斷！

9. 【零註解與保留縮排】`replace` 區塊【絕對嚴禁添加任何自創註解】（如 `// 修正`、`// 優化`），每一行代碼必須 100% 精確保留原始縮排！

10. 【代碼 Search 100% 精確匹配】`search` 區塊必須與目標檔案 100% 一字不漏精確匹配（包含縮排、空格、引號），必須提供 5~10 行完整上下文，嚴禁使用 `// ... (省略)` 偷懶！

11. 【審計輸出決策】只准輸出合法 JSON！只要存在偽聚氣、死鎖、猝死循環或代碼漏洞，【必須回傳 MODIFIED】並給出修復 diff；只有所有物理場與幾何均完全真實合規時，方可回傳 PERFECT！
"""

# ============================================================
# 多輪靜態審查模式 (不執行瀏覽器，純 Code Review)
# ============================================================
def run_static_review(target_file, client, model, logger, report_path, max_rounds=225):
    logger.info("="*60)
    logger.info(f"🕵️‍♂️ 啟動多輪靜態代碼審查模式 (目標檔案: {target_file} | 模型: {model} | 最大輪數: {max_rounds})")
    logger.info("="*60)

    conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
    history_logs = []
    needs_full_snapshot = True
    
    is_pro = "pro" in model.lower()
    MAX_TOKEN_THRESHOLD = 900000  # 統一拉高門檻以最大化利用 Prompt Cache 省錢
    should_reset_next = False

    for current_round in range(1, max_rounds + 1):
        logger.info(f"\n【 靜態審查 - 第 {current_round}/{max_rounds} 輪 】")
        
        if should_reset_next:
            logger.info(f"🔄 偵測到 Token 使用量接近臨界值，自動重啟對話快照...")
            conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
            needs_full_snapshot = True
            should_reset_next = False

        if needs_full_snapshot:
            current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
            if not current_js:
                logger.error("❌ 找不到有效的 JavaScript 代碼，靜態審查中止。")
                return
            user_msg = (
                f"```javascript\n{current_js}\n```\n\n"
                f"【第 {current_round} 輪靜態審查請求】\n"
                f"請仔細審查上述 JS 邏輯代碼快照，找出潛在的邏輯漏洞、風水規則衝突或 JavaScript 語法錯誤。\n"
                f"{CORE_RULES_REMINDER}"
            )
            needs_full_snapshot = False
        else:
            user_msg = (
                f"【第 {current_round} 輪靜態審查請求】\n"
                f"（上一輪的修改已成功套用）。\n"
                f"請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。\n"
                f"{CORE_RULES_REMINDER}"
            )
            
        conversation_history.append({"role": "user", "content": user_msg})
        
        # 共通的 AI 請求與套用邏輯
        ai_json, raw_text, prompt_tokens = get_ai_correction_multiturn(client, model, conversation_history, logger)
        if prompt_tokens > MAX_TOKEN_THRESHOLD:
            logger.warning(f"⚠️ 當前 Prompt Token ({prompt_tokens}) 已超過閾值 ({MAX_TOKEN_THRESHOLD})，下一輪將自動重置歷史。")
            should_reset_next = True
        
        if not ai_json:
            logger.error("解析 AI 回覆失敗，準備下一輪硬重置。")
            # 智慧硬重置：避免 Append-Only 爆 Context
            conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
            needs_full_snapshot = True
            time.sleep(2)
            continue
            
        # ⚡ [Cache 優化] 保留助理原始回覆 (raw_text)，觸發 API 端 100% KV Cache 完全命中
        conversation_history.append({"role": "assistant", "content": raw_text})
        
        status = ai_json.get("status", "UNKNOWN")
        reason = ai_json.get("reason", "無說明")
        changes = ai_json.get("changes", [])

        # ⚡ [防閃退保護] 接收到 Token 爆量訊號，立即清空記憶並重新快照
        if status == "CONTEXT_LIMIT":
            logger.warning("🔄 [自救機制] 已清空所有對話歷史，下一輪將重新讀取最新代碼快照！")
            conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
            needs_full_snapshot = True
            time.sleep(2)
            continue
        
        # 標記是否成功套用，用於報告顯示
        applied_success = False

        logger.info(f"📊 靜態審查結果: [{status}]")
        logger.info(f"💡 發現問題與建議: {reason}")
        
        if status == "PERFECT":
            logger.info("🎉 靜態審查未發現問題，代碼已達完美狀態！任務正式結束。")
            history_logs.append({"round": current_round, "status": status, "reason": reason, "changes": []})
            break
            
        elif status == "MODIFIED":
            if not changes:
                continue
                
            logger.info(f"🛠️ AI 提供了 {len(changes)} 處修改建議，正在嘗試套用...")
            applied_success = apply_code_modifications(target_file, changes, logger)
            
            apply_retries = 0
            while not applied_success and apply_retries < 3:
                apply_retries += 1
                logger.warning(f"⚠️ 檔案更新失敗！(嘗試重試 {apply_retries}/3) 尾部追加最新代碼修正提示...")
                
                retry_prompt = (
                    "❌ 修改套用失敗！你在上一輪提供的 search 區塊在當前檔案中找不到完全匹配的字串。\n"
                    "⚠️ 常見錯誤原因：\n"
                    "1. 你省略了原本代碼中的某些參數或顏色碼，導致字串比對失敗。\n"
                    "2. 你在 replace 代碼中私自添加了註解，或破壞了原始的縮排空白。\n"
                    "3. 經過前面的修改，目標代碼已經長得不一樣了。\n"
                    "請『一字不漏』地複製當前最新代碼作為 search，並提供 5~10 行上下文，replace 中【嚴禁添加任何註解】並嚴格保留原始縮排，重新提供正確的 JSON。"
                )
                conversation_history.append({"role": "user", "content": retry_prompt})
                
                retry_json, retry_raw, _ = get_ai_correction_multiturn(client, model, conversation_history, logger)
                if not retry_json:
                    break
                    
                conversation_history.append({"role": "assistant", "content": retry_raw})
                
                if retry_json.get("status") == "MODIFIED":
                    retry_changes = retry_json.get("changes", [])
                    if retry_changes:
                        applied_success = apply_code_modifications(target_file, retry_changes, logger)
                        if applied_success: changes = retry_changes # 更新報告顯示用
                    else:
                        break
                else:
                    break

            if applied_success:
                logger.info("⏳ 檔案已更新，等待 2 秒後啟動下一輪審查...")
                time.sleep(2)
            else:
                logger.error("🛑 連續 3 次檔案更新失敗，強制進入下一輪硬重置...")
                conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
                needs_full_snapshot = True
                time.sleep(2)

        history_logs.append({
            "round": current_round,
            "status": status,
            "reason": reason,
            "changes": changes if applied_success else [] # 只記錄成功套用的變更
        })

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 3D 風水模擬器 多輪靜態審查報告 (Static Review)\n\n")
        f.write(f"- **目標檔案**：`{target_file}`\n")
        f.write(f"- **調校模型**：`{model}`\n")
        f.write(f"- **總審查輪數**：{len(history_logs)}\n")
        f.write(f"- **最終狀態**：{'🎉 完美狀態' if (history_logs and history_logs[-1]['status'] == 'PERFECT') else '⚠️ 中途終止或達上限'}\n\n")
        f.write(f"## 歷程明細\n\n")
        for item in history_logs:
            f.write(f"### 第 {item['round']} 輪 - [{item['status']}]\n")
            f.write(f"- **診斷說明**：{item['reason']}\n")
            if item.get("changes"):
                f.write(f"- **修改片段細節** ({len(item['changes'])} 處)：\n\n")
                for c_idx, change in enumerate(item["changes"]):
                    f.write(f"  * **修改片段 #{c_idx+1}**:\n")
                    f.write(f"```diff\n")
                    for line in change.get("search", "").strip().split("\n"):
                        f.write(f"- {line}\n")
                    for line in change.get("replace", "").strip().split("\n"):
                        f.write(f"+ {line}\n")
                    f.write(f"```\n\n")
            f.write("\n")
            
    logger.info(f"📝 已自動生成多輪靜態審查摘要報告：{report_path}")

# ============================================================
# 主程式 (動態命名 + 靜態/動態模式路由)
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="3D 模擬器多輪對話自動 Dry Run 與 AI 修正工具")
    parser.add_argument("--file", type=str, required=True, help="HTML 檔案路徑")
    parser.add_argument("--rounds", type=int, default=225, help="最高執行幾輪修正循環")
    parser.add_argument("--runs-per-round", type=int, default=10, help="每一輪執行幾次 Dry Run 取樣")
    parser.add_argument("--model", type=str, default="deepseek-v4-flash", help="API 模型名稱")
    parser.add_argument("--static", action="store_true", help="啟用靜態代碼審查模式 (不執行瀏覽器，僅循環審查代碼)")
    parser.add_argument("--timeout", type=int, default=300, help="單次 API 超時時間（秒）")
    parser.add_argument("--gemini", "--google", action="store_true", dest="gemini", help="★ 使用 Google AI Studio Gemini 端點 (https://generativelanguage.googleapis.com/v1beta/openai/)")
    parser.add_argument("--nvidia", "--nim", action="store_true", dest="nvidia", help="★ 使用 NVIDIA NIM (build.nvidia.com) GLM-5.2 端點")
    parser.add_argument("--dots", nargs="?", const="dots-studio/dots-3-note-preview:free", type=str, default=None, help="★ 使用 OpenRouter Dots3-Note Preview 免費模型 (https://openrouter.ai/api/v1)")
    parser.add_argument("--free-glm", "--glm5", action="store_true", dest="free_glm", help="★ 使用 OpenRouter Free GLM 5.2 免費模型端點 (https://openrouter.ai/api/v1)")
    parser.add_argument("--opencode", "--go", action="store_true", dest="opencode", help="★ 使用 OpenCode Go 訂閱端點 (https://opencode.ai/zen/go/v1)")
    parser.add_argument("--zen", action="store_true", help="使用 OpenCode Zen 按量計費端點 (https://opencode.ai/zen/v1)")
    parser.add_argument("--glm", "--glm53", nargs="?", const="glm-5.3", type=str, default=None, help="使用 OpenCode GLM 模型 (預設 glm-5.3，自動啟用 OpenCode Go)")
    parser.add_argument("--kimi", nargs="?", const="kimi-k3", type=str, default=None, help="使用 OpenCode Kimi 模型 (預設 kimi-k3，自動啟用 OpenCode Go)")
    parser.add_argument("--muse", "--spark", nargs="?", const="muse-spark-1.2-contributor-free", type=str, default=None, help="★ 使用 OpenCode Muse Spark 1.2 模型 (預設 muse-spark-1.2-contributor-free)")
    parser.add_argument("--ox", "--ox-opencode", nargs="?", const="x-preview-f-free", type=str, default=None, help="★ 使用 OpenCode Zen / Go Ox Alpha 模型 (預設 x-preview-f-free)")
    parser.add_argument("--ox-stealth", "--ox-or", "--ox-alpha", "--alpha", nargs="?", const="stealth/ox-alpha", type=str, default=None, help="★ 使用 OpenRouter Stealth Ox-Alpha 模型 (預設 stealth/ox-alpha，端點走 OpenRouter)")
    parser.add_argument("--base-url", type=str, default=None, help="自訂 API Base URL")
    parser.add_argument("--api-key", type=str, default=None, help="直接指定 API Key 字串")
    parser.add_argument("--api-key-file", type=str, default=None, help="指定 API Key 檔案路徑")
    args = parser.parse_args()

    # 快捷模型覆寫
    if args.dots:
        dots_val = args.dots.strip()
        if not ("/" in dots_val):
            dots_val = f"dots-studio/{dots_val}"
        if not (dots_val.endswith(":free") or dots_val.endswith(":preview")):
            dots_val = f"{dots_val}:free"
        args.model = dots_val
    elif args.free_glm and args.model == "deepseek-v4-flash":
        args.model = "z-ai/glm-5.2:free"
    elif args.glm:
        args.model = args.glm
    elif args.kimi:
        args.model = args.kimi
    elif args.muse:
        args.model = args.muse
    elif args.ox_stealth:
        ox_val = args.ox_stealth.strip()
        if not ("/" in ox_val):
            ox_val = f"stealth/{ox_val}"
        args.model = ox_val
    elif args.ox:
        ox_val = args.ox.strip()
        if "stealth" in ox_val.lower() or ox_val.lower() in ["or", "openrouter"]:
            args.model = "stealth/ox-alpha"
        elif ox_val in ["ox", "ox-alpha", "ox-alpha-free", "alpha", "opencode", "zen"]:
            args.model = "x-preview-f-free"
        else:
            args.model = ox_val

    # OpenCode API 端點要求純模型 ID (不可包含 opencode/ 或 opencode-go/ 前綴)
    if args.model.startswith("opencode/"):
        args.model = args.model[len("opencode/"):]
    elif args.model.startswith("opencode-go/"):
        args.model = args.model[len("opencode-go/"):]

    base_name = os.path.splitext(os.path.basename(args.file))[0]
    log_file = f"auto_tuner_{base_name}_log.txt"
    report_path = f"tuning_report_{base_name}.md"

    logger = setup_logger(log_file)

    target_file = args.file
    if not os.path.exists(target_file):
        logger.error(f"❌ 找不到目標檔案: {target_file}")
        return

    # 初始化 API 提供商端點與模型
    is_gemini_provider = args.gemini
    is_nvidia_provider = args.nvidia
    is_openrouter_provider = bool(args.dots) or args.free_glm or bool(args.ox_stealth) or ("stealth" in args.model.lower()) or ("openrouter" in str(args.base_url or "").lower())
    is_opencode_provider = (args.opencode or args.zen or bool(args.glm) or bool(args.kimi) or bool(args.muse) or bool(args.ox)) and not is_openrouter_provider

    if args.gemini:
        base_url = args.base_url or "https://generativelanguage.googleapis.com/v1beta/openai/"
        provider_name = "Google AI Studio (Gemini Free 每日 1500 額度)"
        if args.model == "deepseek-v4-flash":
            args.model = "gemini-flash-latest"
    elif args.nvidia:
        base_url = args.base_url or "https://integrate.api.nvidia.com/v1"
        provider_name = "NVIDIA NIM GLM-5.2 (build.nvidia.com)"
        if args.model == "deepseek-v4-flash":
            args.model = "z-ai/glm-5.2"
    elif args.free_glm:
        base_url = args.base_url or "https://openrouter.ai/api/v1"
        provider_name = "OpenRouter Free GLM 5.2 (https://openrouter.ai/api/v1)"
        if args.model == "deepseek-v4-flash":
            args.model = "z-ai/glm-5.2:free"
    elif is_openrouter_provider:
        base_url = args.base_url or "https://openrouter.ai/api/v1"
        provider_name = f"OpenRouter ({args.model})"
    elif args.zen or (bool(args.muse) and not args.opencode) or (bool(args.ox) and not is_openrouter_provider and not args.opencode):
        base_url = args.base_url or "https://opencode.ai/zen/v1"
        provider_name = f"OpenCode Zen ({args.model})"
    elif is_opencode_provider:
        base_url = args.base_url or "https://opencode.ai/zen/go/v1"
        provider_name = f"OpenCode Go 訂閱端點 ({args.model})"
    else:
        base_url = args.base_url or "https://api.deepseek.com"
        provider_name = "DeepSeek 官方 API"

    key_pool = load_api_keys(
        key_file=args.api_key_file,
        api_key_str=args.api_key,
        is_opencode=is_opencode_provider,
        is_openrouter=is_openrouter_provider,
        is_free_glm=args.free_glm,
        is_gemini=is_gemini_provider,
        is_nvidia=is_nvidia_provider
    )
    if not key_pool:
        logger.error("❌ 找不到 API Key，請建立金鑰檔案或設定環境變數！")
        return

    init_key = key_pool.get_current_key()
    logger.info(
        f"API 提供商: {provider_name} ({base_url}) | 模型: {args.model} | "
        f"金鑰池: 共載入 {len(key_pool)} 把 Key (初始: {key_pool.mask_key(init_key)})"
    )
    client = OpenAI(api_key=init_key, base_url=base_url, timeout=args.timeout)
    client.key_pool = key_pool

    if args.static:
        run_static_review(target_file, client, args.model, logger, report_path, args.rounds)
        return

    # ==========================
    # 以下為先靜後動實測循環模式 (連續2次靜態完美 -> 動態實測 -> 畢業大考)
    # ==========================
    is_pro = "pro" in args.model.lower()
    MAX_ROUNDS = args.rounds
    RUNS_PER_ROUND = max(args.runs_per_round, 20) # 提高平時取樣基數，加快暴露出問題
    EXAM_RUNS = 100 if is_pro else 60             # 大幅提升大考壓測量，確保抓出 1% 低機率的 Bug
    
    # ⚡ [省錢調校] 靜態審查縮短為 2 輪，避免反覆罰坐 5 輪燃燒昂貴的思考鏈 (Reasoning Tokens)
    STATIC_TARGET = 2                             # 靜態審查通過門檻 (2 輪即達標)
    SUCCESS_TARGET = STATIC_TARGET + 3            # 總通關目標：2 輪靜態 + 2 輪動態 + 1 輪畢業大考 = 5 關
    
    MAX_TOKEN_THRESHOLD = 900000  # 統一拉高門檻以最大化利用 Prompt Cache 省錢
    should_reset_next = False
    
    consecutive_perfects = 0
    history_logs = []
    syntax_error_retries = 0

    conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
    needs_full_snapshot = True 

    logger.info("="*60)
    logger.info(f"🚀 開始多輪對話自動化調校任務 ({STATIC_TARGET}次靜態 -> 動態實測高快取模式) (目標檔案: {target_file} | 模型: {args.model})")
    logger.info("="*60)

    # 封裝修改套用與重試邏輯，供主循環與大考共用
    def try_apply_with_retries(target_file, changes, conversation_history, client, model, logger):
        success = apply_code_modifications(target_file, changes, logger)
        apply_retries = 0
        final_changes = changes
        while not success and apply_retries < 3:
            apply_retries += 1
            logger.warning(f"⚠️ 檔案更新失敗！(嘗試重試 {apply_retries}/3) 尾部追加最新代碼修正提示...")
            
            retry_prompt = (
                "❌ 修改套用失敗！你在上一輪提供的 search 區塊在當前檔案中找不到完全匹配的字串。\n"
                "⚠️ 常見錯誤原因：\n"
                "1. 你省略了原本代碼中的某些參數或顏色碼，導致字串比對失敗。\n"
                "2. 你在 replace 代碼中私自添加了註解，或破壞了原始的縮排空白。\n"
                "3. 經過前面的修改，目標代碼已經長得不一樣了。\n"
                "請『一字不漏』地複製當前最新代碼作為 search，並提供 5~10 行上下文，replace 中【嚴禁添加任何註解】並嚴格保留原始縮排，重新提供正確的 JSON。"
            )
            
            conversation_history.append({"role": "user", "content": retry_prompt})
            retry_json, retry_raw, _ = get_ai_correction_multiturn(client, model, conversation_history, logger)
            
            if not retry_json:
                break
            
            # ⚡ [Cache 優化] 保留助理原始回覆，確保前綴一字不漏匹配
            conversation_history.append({"role": "assistant", "content": retry_raw})
            if retry_json.get("status") == "MODIFIED":
                retry_changes = retry_json.get("changes", [])
                if retry_changes:
                    success = apply_code_modifications(target_file, retry_changes, logger)
                    if success: final_changes = retry_changes
                else:
                    break
            else:
                break
        return success, final_changes

    for current_round in range(1, MAX_ROUNDS + 1):
        # ⚡ [策略優化] 連續通過 STATIC_TARGET 輪靜態審查後直接進入動態實測；最後一關為畢業大考
        is_static = (consecutive_perfects < STATIC_TARGET)
        is_exam = (not is_static) and (consecutive_perfects >= (SUCCESS_TARGET - 1))
        
        mode_name = "靜態審查" if is_static else ("動態大考" if is_exam else "動態實測")
        logger.info(f"\n【 第 {current_round}/{MAX_ROUNDS} 輪測試 - {mode_name} 】連續完美次數: {consecutive_perfects}/{SUCCESS_TARGET}")
        
        # ⚡ 保持 messages[0] 完全不變，維持 Prompt Cache 命中率！

        # 🔄 Token 自動重置機制
        if should_reset_next:
            logger.info(f"🔄 偵測到 Token 使用量接近臨界值，自動重啟對話快照...")
            conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
            needs_full_snapshot = True 
            should_reset_next = False 

        if is_static:
            if needs_full_snapshot:
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                user_msg = (
                    f"```javascript\n{current_js}\n```\n\n"
                    f"【第 {current_round} 輪 - 靜態審查請求】\n"
                    f"請仔細審查上述 JS 邏輯代碼快照，找出潛在的邏輯漏洞、風水規則衝突或 JavaScript 語法錯誤。\n"
                    f"{CORE_RULES_REMINDER}"
                )
                needs_full_snapshot = False
            else:
                user_msg = (
                    f"【第 {current_round} 輪 - 靜態審查請求】\n"
                    f"（上一輪修改已生效）。請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。\n"
                    f"{CORE_RULES_REMINDER}"
                )
                
            conversation_history.append({"role": "user", "content": user_msg})
        else:
            runs = EXAM_RUNS if is_exam else RUNS_PER_ROUND
            if is_exam:
                logger.info(f"🎓 進入【畢業大考階段】！正在啟動 {EXAM_RUNS} 次高強度高壓測試 (涵蓋所有預設案例與極端組合)...")
                
            run_results = run_browser_simulations(target_file, runs, logger, is_exam)
            
            if isinstance(run_results, tuple) and run_results[0] == "JS_ERROR":
                js_err_msg = run_results[1]
                syntax_error_retries += 1
                
                if syntax_error_retries > 3:
                    logger.error("🛑 連續 3 次語法自我修復失敗，自動終止任務並復原檔案。")
                    rollback_file(target_file, logger)
                    break
                    
                logger.warning(f"⚠️ 偵測到 JavaScript 語法崩潰 (嘗試自我修復 {syntax_error_retries}/3)...")
                rollback_file(target_file, logger) 
                
                err_user_msg = (
                    f"【語法崩潰緊急修復】上一輪套用修改後爆發了以下 JavaScript 語法錯誤：\n```\n{js_err_msg}\n```\n"
                    f"檔案已自動復原至備份檔。請重新檢視並提供不含語法錯誤的修正 JSON。\n"
                    f"{CORE_RULES_REMINDER}"
                )
                conversation_history.append({"role": "user", "content": err_user_msg})

                fix_json, raw_text, _ = get_ai_correction_multiturn(client, args.model, conversation_history, logger)
                
                if fix_json and fix_json.get("status") == "MODIFIED":
                    conversation_history.append({"role": "assistant", "content": raw_text})
                    fix_changes = fix_json.get("changes", [])
                    logger.info(f"🩹 AI 重新診斷並提供了 {len(fix_changes)} 處語法修正方案，嘗試重新套用...")
                    success, fix_changes = try_apply_with_retries(target_file, fix_changes, conversation_history, client, args.model, logger)
                    
                    history_logs.append({
                        "round": current_round,
                        "status": "SYNTAX_FIX",
                        "reason": f"自我修復語法錯誤: {js_err_msg}",
                        "changes": fix_changes if success else []
                    })
                else:
                    logger.error("AI 未能提供有效的語法修復方案。")
                    
                consecutive_perfects = 0
                time.sleep(2)
                continue
            else:
                syntax_error_retries = 0

            # 接收 Tuple (Status, unique_results, passed_core_count)
            if not run_results or (isinstance(run_results, tuple) and run_results[0] != "SUCCESS"):
                logger.error("無法收集到 Dry Run 數據，跳過此輪。")
                consecutive_perfects = 0
                time.sleep(2)
                continue

            _, unique_results, passed_core_count = run_results
            logger.info(f"  ✂️ 數據收集完成：傳送 {len(unique_results)} 筆高價值特徵 (隱藏 {passed_core_count} 筆防迴歸完美數據)")

            # ================= 終端機高亮顯示幾何與物理異常 =================
            # ⚡ [修復 Bug] 將 run_results 改為 unique_results
            for res in unique_results:
                if isinstance(res, dict) and res.get("sanityWarnings"):
                    for warn in res["sanityWarnings"]:
                        if "幾何" in warn or "物理異常" in warn or "穿模" in warn:
                            logger.error(f"📐 🚨 【幾何與物理嚴重警告】: {warn}")
                        elif "光學異常" in warn:
                            logger.error(f"🎨 🚨 【光學與色彩嚴重警告】: {warn}")
                
                if isinstance(res, dict):
                    spatial = res.get("lairInfo", {}).get("spatialProfile", {})
                    anomalies = spatial.get("geometryAnomalies", [])
                    for anomaly in anomalies:
                        logger.warning(f"⛰️ 空間與物理掃描: {anomaly}")
            # ================================================================

            # ⚡ [防迴歸機制] 檢查是否有原本應為完美的經典格局在本次修改後出現異常
            regression_warnings = []
            for res in unique_results:
                preset = res.get("presetName", "")
                warnings = res.get("sanityWarnings", [])
                if preset and not preset.startswith("random") and len(warnings) > 0:
                    regression_warnings.append(f"  - 🚨【防迴歸嚴重警告】：經典預設格局 [{preset}] 出現異常警告：{warnings}")
            
            regression_text = ("⚠️⚠️⚠️【偵測到代碼修改引發功能倒退 (Regression)】：\n" + "\n".join(regression_warnings) + "\n\n") if regression_warnings else ""

            # ⚡ [Cache 優化] 加入 sort_keys=True 確保 JSON 結構鍵值順序 100% 決定論
            compact_json = json.dumps(unique_results, separators=(',', ':'), ensure_ascii=False, sort_keys=True)
            
            summary_text = f"✅ 已在背景默默通過 {passed_core_count} 項經典防迴歸測試，未發現異常（已隱藏其詳細 JSON 以節省 Token 空間）。\n\n" if passed_core_count > 0 else ""

            if needs_full_snapshot:
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = (
                    f"```javascript\n{current_js}\n```\n\n"
                    f"【第 {current_round} 輪 - {prefix}請求】\n"
                    f"{regression_text}"
                    f"{summary_text}【高價值測試數據 (僅列出隨機邊界與異常案例)】:\n{compact_json}\n"
                    f"{CORE_RULES_REMINDER}"
                )
                needs_full_snapshot = False
            else:
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = (
                    f"【第 {current_round} 輪 - {prefix}請求】\n"
                    f"（上一輪修改已生效）。\n"
                    f"{regression_text}"
                    f"{summary_text}【高價值測試數據 (僅列出隨機邊界與異常案例)】:\n{compact_json}\n"
                    f"{CORE_RULES_REMINDER}"
                )
            
            conversation_history.append({"role": "user", "content": user_msg})

        # 共通的 AI 請求與套用邏輯
        ai_json, raw_text, prompt_tokens = get_ai_correction_multiturn(client, args.model, conversation_history, logger)
        if client.key_pool and client.key_pool.is_all_dead():
            logger.critical("🛑 金鑰池已無可用金鑰，流水線立即中止！")
            break

        if prompt_tokens > MAX_TOKEN_THRESHOLD:
            logger.warning(f"⚠️ 當前 Prompt Token ({prompt_tokens}) 已超過閾值 ({MAX_TOKEN_THRESHOLD})，下一輪將自動重置歷史。")
            should_reset_next = True
        
        if not ai_json:
            logger.error("解析 AI 回覆失敗，準備硬重置對話。")
            conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
            needs_full_snapshot = True
            consecutive_perfects = 0
            time.sleep(3)
            continue
            
        # ⚡ [Cache 優化] 保留助理原始回覆 (raw_text)，觸發 API 端 100% KV Cache 完全命中
        conversation_history.append({"role": "assistant", "content": raw_text})

        status = ai_json.get("status", "UNKNOWN")
        reason = ai_json.get("reason", "無說明")
        changes = ai_json.get("changes", [])

        # ⚡ [防閃退保護] 接收到爆量訊號，立即清空記憶重新快照
        if status == "CONTEXT_LIMIT":
            logger.warning("🔄 [自救機制] 已清空所有對話歷史，下一輪將重新讀取最新代碼快照！")
            conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
            needs_full_snapshot = True
            time.sleep(2)
            continue
            
        applied_success = False
        
        logger.info(f"📊 AI 診斷結果: [{status}]")
        logger.info(f"💡 AI 說明: {reason}")
        
        if status == "PERFECT":
            consecutive_perfects += 1
            
            if consecutive_perfects == SUCCESS_TARGET:
                logger.info(f"🎉 通過全部 {SUCCESS_TARGET} 階段考驗 ({STATIC_TARGET}輪靜態 + {SUCCESS_TARGET - STATIC_TARGET}輪動態)！系統已達完美穩定狀態！任務正式結束。")
                history_logs.append({"round": current_round, "status": "PERFECT", "reason": f"通過 {SUCCESS_TARGET} 階段最終大考", "changes": []})
                break
            else:
                next_is_static = (consecutive_perfects < STATIC_TARGET)
                next_mode = "靜態審查" if next_is_static else ("畢業大考" if consecutive_perfects >= (SUCCESS_TARGET - 1) else "動態實測")
                logger.info(f"✅ 本輪判定通過 (已連續成功 {consecutive_perfects}/{SUCCESS_TARGET} 次)，準備進入第 {consecutive_perfects + 1} 階段: 【{next_mode}】...")
                time.sleep(1)
                
        elif status == "MODIFIED":
            consecutive_perfects = 0
            if changes:
                logger.info(f"🛠️ AI 提供了 {len(changes)} 處代碼修改建議，正在嘗試套用...")
                success, final_changes = try_apply_with_retries(target_file, changes, conversation_history, client, args.model, logger)
                applied_success = success
                changes = final_changes

                if success:
                    logger.info("⏳ 檔案已更新，等待 2 秒後啟動新的一輪測試...")
                    time.sleep(2)
                else:
                    logger.error("🛑 連續 3 次檔案更新失敗，將強制硬重置對話...")
                    conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
                    needs_full_snapshot = True 
                    time.sleep(2)
        else:
            consecutive_perfects = 0

        history_logs.append({
            "round": current_round,
            "status": status,
            "reason": reason,
            "changes": changes if applied_success else []
        })

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 3D 風水模擬器 多輪先靜後動調校報告 (Static then Dynamic Run)\n\n")
        f.write(f"- **目標檔案**：`{target_file}`\n")
        f.write(f"- **調校模型**：`{args.model}`\n")
        f.write(f"- **總測試輪數**：{len(history_logs)}\n")
        f.write(f"- **最終狀態**：{'🎉 已達完美穩定狀態 (通過 ' + str(SUCCESS_TARGET) + ' 階段考驗)' if consecutive_perfects >= SUCCESS_TARGET else '⚠️ 中途終止或達到最大輪數'}\n\n")
        f.write(f"## 歷程明細\n\n")
        for item in history_logs:
            f.write(f"### 第 {item['round']} 輪 - [{item['status']}]\n")
            f.write(f"- **診斷說明**：{item['reason']}\n")
            if item.get("changes"):
                f.write(f"- **修改片段細節** ({len(item['changes'])} 處)：\n\n")
                for c_idx, change in enumerate(item["changes"]):
                    f.write(f"  * **修改片段 #{c_idx+1}**:\n")
                    f.write(f"```diff\n")
                    for line in change.get("search", "").strip().split("\n"):
                        f.write(f"- {line}\n")
                    for line in change.get("replace", "").strip().split("\n"):
                        f.write(f"+ {line}\n")
                    f.write(f"```\n\n")
            f.write("\n")
            
    logger.info(f"📝 已自動生成多輪先靜後動調校報告：{report_path}")

if __name__ == "__main__":
    main()