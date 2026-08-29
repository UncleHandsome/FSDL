import os
import re
import json
import time
import random
import logging
import argparse
import shutil
import base64
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

    def _normalize_dict(obj):
        """保證回傳 dict，防止 AI 回傳 list 結構導致 .get() 崩潰"""
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and "status" in item:
                    return item
            if any(isinstance(item, dict) and "search" in item for item in obj):
                return {"status": "MODIFIED", "reason": "AI 回傳陣列形式之修改建議", "changes": obj}
            if len(obj) > 0 and isinstance(obj[0], dict):
                return obj[0]
        return None

    def _parse_candidate(raw_cand):
        if not raw_cand:
            return None
        cand = raw_cand.strip()
        # 1. 優先嘗試非嚴格解析 (容許字串內有未跳脫的換行符與控制字元)
        try:
            return _normalize_dict(json.loads(cand, strict=False))
        except Exception:
            pass
        # 2. 自動修復常見的尾隨逗號 (Trailing Commas)
        try:
            cleaned = re.sub(r',\s*([\]}])', r'\1', cand)
            return _normalize_dict(json.loads(cleaned, strict=False))
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
def run_browser_simulations(html_path, num_runs, logger, is_exam=False, capture_screens=False, shots_dir=None):
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

        # 📸 圖片模式：每筆有效樣本 Dry Run 完成後擷取模擬器實際渲染畫面，交給 AI 視覺判讀
        if capture_screens and shots_dir:
            os.makedirs(shots_dir, exist_ok=True)
        shot_seq = [0]

        def snap(page_ref, res=None, label="sample"):
            if not capture_screens or not shots_dir:
                return None
            try:
                page_ref.wait_for_timeout(500)  # 等待渲染迴圈繪出最新模擬狀態
                shot_seq[0] += 1
                fname = f"{label}_{shot_seq[0]:02d}.jpg"
                fpath = os.path.join(shots_dir, fname)
                page_ref.screenshot(path=fpath, type="jpeg", quality=80)
                logger.info(f"    📸 已擷取畫面: {fname}")
                if isinstance(res, dict):
                    res["_shot"] = fname
                return fname
            except Exception as shot_err:
                logger.warning(f"    ⚠️ 畫面擷取失敗: {shot_err}")
                return None
        
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
                    return DryRunTool.executeSingle(20, true, true);
                } else if (window.DryRunTool && window.DryRunTool.executeSingle) {
                    return window.DryRunTool.executeSingle(20, true, true);
                }
                return null;
            })()
            """

            if is_exam:
                logger.info("  ▶ [階段 1] 執行全格局覆蓋測試 (executeSuite)...")
                exam_script = """
                (() => {
                    if (typeof DryRunTool !== 'undefined' && DryRunTool.executeSuite) {
                        return DryRunTool.executeSuite(15);
                    } else if (window.DryRunTool && window.DryRunTool.executeSuite) {
                        return window.DryRunTool.executeSuite(15);
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
                        
                        if "上吉" in expected:
                            rating_matches = ("上吉" in actual)
                        else:
                            rating_matches = (expected == "動態判定") or any(exp in actual for exp in expected.split("/"))

                        gather_val = res.get("gatherAcc", 0)
                        is_auspicious = preset_name in ['perfect', 'diwang', 'shixiang', 'zuozhang', 'huilong', 'bendi']
                        is_gather_dead = is_auspicious and (gather_val < 15)

                        if is_fixed and not has_warnings and rating_matches and not is_gather_dead:
                            passed_core_count += 1
                            continue

                        res_hash = clean_and_hash(res)
                        if res_hash not in seen_hashes:
                            seen_hashes.add(res_hash)
                            valid_results.append(res)
                if capture_screens:
                    snap(page, None, label="exam_overview")  # 大考為批次套件，額外擷取一張總覽畫面
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
                        # ⚡ [修復] 箭頭函數必須用 () => (表達式) 包裹 dry_run_script；
                        # 舊寫法 () => {dry_run_script} 是區塊體且無 return，永遠回傳 undefined，
                        # 導致一般模式固定格局防迴歸測試被整批靜默跳過
                        res = page.evaluate(f"""
                            Promise.race([
                                Promise.resolve().then(() => ({dry_run_script})),
                                new Promise((_, reject) => setTimeout(() => reject(new Error('Dry Run 超時 (可能存在死迴圈)')), 60000))
                            ])
                        """)
                    except Exception as eval_err:
                        logger.error(f"  ❌ 執行 Dry Run 評估失敗或超時: {eval_err}")
                        res = None
                    if not res: continue

                    has_warnings = len(res.get("sanityWarnings", [])) > 0
                    expected = res.get("expectedRating", "動態判定")
                    actual = res.get("scoringAndVerdict", {}).get("verdictRating", "") or res.get("verdict", {}).get("rating", "") or res.get("verdict", "")
                    
                    # 嚴格斷言：若期望為頂級大吉格局，實際評級不可退化至中平，且聚氣量不得衰減至枯竭
                    if "上吉" in expected:
                        rating_matches = ("上吉" in actual)
                    else:
                        rating_matches = (expected == "動態判定") or any(exp in actual for exp in expected.split("/"))

                    gather_val = res.get("gatherAcc", 0)
                    is_auspicious_preset = preset in ['perfect', 'diwang', 'shixiang', 'zuozhang', 'huilong', 'bendi']
                    is_gather_dead = is_auspicious_preset and (gather_val < 15)

                    if not has_warnings and rating_matches and not is_gather_dead:
                        passed_core_count += 1
                        continue

                    res_hash = clean_and_hash(res)
                    if res_hash not in seen_hashes:
                        seen_hashes.add(res_hash)
                        snap(page, res)
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
                        snap(page, res)
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
        max_tokens_val = 65536
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
                # OpenCode Zen / Go 專屬 Responses API 協議轉發 (開啟高強度思考並適配多模態圖片輸入)
                try:
                    responses_input = []
                    for msg in conversation_history:
                        role = msg.get("role", "user")
                        content = msg.get("content")
                        if isinstance(content, list):
                            new_content = []
                            for part in content:
                                if isinstance(part, dict):
                                    p_type = part.get("type", "")
                                    if p_type == "text":
                                        new_content.append({"type": "input_text", "text": part.get("text", "")})
                                    elif p_type == "image_url":
                                        img_val = part.get("image_url", "")
                                        img_url = img_val.get("url", "") if isinstance(img_val, dict) else str(img_val)
                                        new_content.append({"type": "input_image", "image_url": img_url})
                                    elif p_type in ["input_text", "input_image"]:
                                        new_content.append(part)
                                    else:
                                        new_content.append(part)
                                else:
                                    new_content.append(part)
                            responses_input.append({"role": role, "content": new_content})
                        else:
                            responses_input.append(msg)

                    resp_data = client.post(
                        "responses",
                        cast_to=object,
                        body={
                            "model": model,
                            "input": responses_input,
                            "temperature": 1.0,
                            "max_output_tokens": min(max_tokens_val, 65536),
                            "reasoning": {"effort": "xhigh"},
                            "reasoning_effort": "xhigh",
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
我們的對話將維持連續歷史紀錄。你將會收到四種類型的診斷請求：
1. 【動態 Dry Run 診斷】：分析傳入的多輪測試數據 JSON。
2. 【靜態代碼審查】：分析傳入的最新 JS 代碼快照。
3. 【語法崩潰修復】：修改引發 JavaScript 錯誤、檔案已回滾後的緊急重建。
4. 【畫面視覺審查（圖片模式限定）】：分析隨測試數據附帶的模擬器渲染截圖，判斷畫面是否合理並與 JSON 數據交叉驗證。

🔥🔥🔥 【數據契約（嚴禁假設任何欄位存在）】 🔥🔥🔥
審計前必須先通讀數據 JSON 實際擁有的鍵名，一切判斷只基於真實存在的欄位：
- 最終評級可能叫 `verdict.rating`（3D 巒頭模）或 `scoringAndVerdict.verdictRating`（城市模），以實際出現者為準，兩者嚴禁混寫。
- 粒子模與城市模的 Dry Run 頂層均已提供 presetName / expectedRating / sanityWarnings / gatherAcc / scatterAcc / physicsStability（含 particleCount 與百分比字串 capacityUtilization）；城市模另有 scoringAndVerdict（其 gatherRaw/scatterRaw 為同源原始值）、hallSaturationRate 等自有欄位。
- 【架構適用性】若當前檔案為 GPU 無狀態流式架構（數據無 pState 狀態機與 gatherAcc/scatterAcc 累加器，僅有每幀解析值），則第 1 條狀態機遲滯與死穴 2/3 一律宣告 N/A，嚴禁強行腦補映射到等價物；審計改以每幀解析值之極值與一致性判斷。宣告 N/A 只需在 reason 說明一句架構依據，之後各輪直接引用，禁止重複長篇論證。
- 城市模若提供 `yinYangBalance`（理論域 [-10, 10]），僅可作輔助判據：趨近 ±10 為陰陽極端失衡；數據中無此欄位時嚴禁腦補引用。
- 數據中不存在的指標一律不得引用，更不得據不存在的欄位判定 MODIFIED。

🔥🔥🔥 【湧現物理與風水本體論（嚴禁任何字串與屬性硬編碼）】 🔥🔥🔥
風水物理引擎嚴禁依賴任何輸入參數標籤（如嚴禁在動力學中新寫 `if (state.ms === 'sunken')`、`if (state.terrain === 'fanpo')` 或 `if (state.w === 'tfork')`）。
所有風水吉凶必須 100% 由「空間拓撲幾何」、「CFD 向量流場」與「局部動力學微分特性」自然湧現！
【範圍限定】此禁令針對「新增或改寫」的動力學與評分邏輯；既有的配置映射表（形煞名稱清單、預設格局參數等）屬聲明式資料而非作弊分支，嚴禁發動大型重構去剷除它們。【邊界澄清】修補時被動保留含既有字串判斷的行不視為違規；違規僅指「新引入」字串分支、或把既有物理湧現邏輯改寫成字串分支。

1. 🚨【粒子狀態機雙門檻遲滯（Deposition–Entrainment Hysteresis）】:
   - 《葬書》「氣乘風則散，界水則止；聚之使不散，行之使有止」——氣必須能微行（非微風即散），又必須能駐留（非強煞不破）。狀態機必須是雙門檻遲滯結構：「再分散門檻」必須高於「凝結門檻」，這是真實風沙/沉積體系的 Shields 迴滯律！
   - 【量綱鐵律】環境風速平方（windSpeedSq = wx*wx + wz*wz）與載體粒子速度平方（vel.lengthSq()）是兩個不同物理量，對應不同門檻，嚴禁混用同一變數敘述：
     * 【凝結（pState=2）】載體 vel.lengthSq() < 2.0 且局部亂流 turbulence < 1.5 且處於近地懸浮層（地形高 + 0.5 ≤ y ≤ 地形高 + 3.0，立體雙螺旋盤旋），且位於乾燥陸地、非陷阱區、無致命煞場。
     * 【自由氣吹散（pState=0/3）】環境強風（windSpeedSq > 2.0~2.5 或遭遇地形強剪切）才在行度中剝離未凝之氣；正常山脊微風與重力導引不應將龍脈生氣中途全數扼殺。
     * 【已凝吉氣剝離（遲滯帶）】已凝結的 pState=2 唯有在 windSpeedSq > 3.5 或 turbulence > 3.5 時才允許被打散——沉積易、再懸浮難。
     * 【紅煞（pState=1）】turbulence > 5.5 方轉紅煞。
   - 嚴禁把任一量綱軸上的「再分散門檻」調低至該軸的「凝結門檻」之下——【遲滯鐵律・僅限同軸比較】：環境風軸上，剝離已凝吉氣之門檻（windSpeedSq > 3.5）必須遠高於吹散自由浮氣之門檻；載體速度軸上，凝結所需沉降動能（vel.lengthSq() < 2.0）必須低於將其再度掀散之局部動能。跨量綱的門檻數值大小比較無物理意義，嚴禁為之！若違反本條，任何戶外微風都會剝離一切已聚之氣，天下無穴，違背藏風聚氣的可實現性！
   - 【時間演化穩定性】物理狀態必須在長時間（15~30 秒以上）運行下維持熱力學動態平衡，嚴禁出現隨時間推移生氣單調衰竭、粒子全滅歸零之時序崩潰。

2. 🚨【界水則止與水法本質（Boundary Layer Stagnation）】:
   - 《葬書》「氣乘風則散，界水則止」：水體是生氣流動的物理邊界。且《葬書》明訂位階「風水之法，得水為上，藏風次之」——審計時水法失誤（界水不止、反弓沖割、水口直瀉）權重重於風法失誤，嚴禁本末倒置。
   - 【物理機制】：慢速環抱水體（玉帶水）在陸水交界處應具備水陸邊界層阻尼，使前進的氣流自然減速沉降於明堂與太極暈前方；僅當局部剪切強度顯著超越全域背景流場（急流沖割的相對比值判準）時，才轉化為水煞。嚴禁將所有水體一律視為負壓黑洞，也嚴禁使用無代碼出處的絕對剪切常量！
   - 【玉帶反弓・真實河流形態學】：彎道水流存在螺旋環流（Helicoidal Flow）——表層流向凹岸（外彎）沖刷侵蝕、底層攜沙移向凸岸（內彎）減速淤積。故穴位於彎道內側（凸岸，水環抱有情）流速緩、剪切弱，為「玉帶環腰」大吉；穴位於彎道外側（凹岸，受主流正面沖射）剪切強、岸腳崩退，為「反弓水／割腳水」大凶。吉凶必須由「穴位與彎道曲率中心之相對幾何 + 局部剪切量測」自然湧現，嚴禁用名稱標籤判定！
   - 【水法曲直與水口】：水貴屈曲之玄（九曲來水、迴環有情），忌直來直去（牽牛水直瀉、箭射水穿堂、水破天心）；水口（去水處）宜緊閉迴繞、有砂交鎖，忌一瀉無收。直瀉則氣隨水散、界水功能失效，對應觀測即穴區淨流入轉負、聚氣無法留存。

3. 🚨【窩穴聚氣 vs 陷坑死水（Topological Curvature & Ventilation）】:
   - 傳統四大正穴（窩、鉗、乳、突）中，「窩穴」為開闊微凹地形，氣流在其中形成舒緩渦旋，屬大吉結穴。
   - 【真偽判定】：由地形局部幾何曲率（二階偏導 $\nabla^2 h$）與通風通量（Flux）客觀判定：
     * **吉（窩穴太極暈）**：盆地開闊（寬深比 $W/D > 4$），微凹聚氣，環境風平靜（windSpeedSq < 0.35）且載體呈層流沉降（vel.lengthSq() < 2.0、turbulence < 1.5）、穴區粒子淨流入為正（進入太極暈範圍之粒子數多於離開者）。
     * **凶（陷煞死坑）**：深度過大且封閉（$W/D < 2$），粒子垂直陷落後無法逃逸，滯留時間過長且呈陰濕死滯信號。
   - 判定必須完全依賴微積分幾何與流體速度，嚴禁寫死名稱過濾！

4. 🚨【山龍與平洋龍全域統一驅動（Geopotential & Drift Unification）】:
   - 形家通說「平洋一突值千金」、「高一寸為山，低一寸為水」。
   - 【物理機制】：粒子推力向量 $\vec{F}_{drive} = -\alpha \nabla h + \beta \vec{v}_{drift}$。
     * 在高山（山龍），地勢坡度梯度 $-\nabla h$ 主導粒子沿山脊向下奔馳。
     * 在平原（平洋龍，$\nabla h \approx 0$），由大氣宏觀背景微壓差與水流牽引向量 $\vec{v}_{drift}$ 主導推進。
   - 兩者共用同一套力學方程式，確保平洋地貌絕不發生流體死鎖（Zero-Activity Stall）。

5. 🚨【理氣全周天連續性與空亡線（Circular Continuous Li Qi）】:
   - 羅盤 $0^\circ \sim 360^\circ$ 為連續圓周流形。八宮（45°/宮）與二十四山（15°/山）判定嚴禁新寫離散 `if-else` 分支。
   - 坐向與方位計算一律使用三角諧波或模運算（`let deg = (rawDeg % 360 + 360) % 360`）。
   - 出卦與空亡線判定（正統理氣三級制，嚴禁相位顛倒）：
     * 令到山心角距 `d_center_shan = |(deg % 15 + 15) % 15 - 7.5|`（山心為 0°，邊界為 7.5°）；
     * 則到山脈分界線距離 `d_to_bound_shan = 7.5 - d_center_shan`（山心為 7.5° 最穩、騎線界線為 0° 空亡）；
     * 令到八宮卦界距離 `d_to_bound_gua = 22.5 - |(deg % 45 + 45) % 45 - 22.5|`（卦心為 22.5° 最吉、卦界邊界為 0° 出卦大空亡）。
     * 【正針】山心兩側 ±4.5° 內（`d_center_shan <= 4.5`，即 `d_to_bound_shan >= 3.0`）為正山正氣，最吉。
     * 【兼向（縫針）】山心兩側 4.5°～7.5°（`d_to_bound_shan` 介於 0°~3°）為兼向，氣帶雜煞，凶度隨接近界線連續遞增。
     * 【小空亡】騎二十四山界線（`d_to_bound_shan -> 0`），陰陽差錯，凶。
     * 【大空亡】騎八宮卦界線（`d_to_bound_gua -> 0`），出卦無氣可乘，為最凶——其凶度基底權重必須顯著高於小空亡，兩層連續場疊加取最大。
   - 嚴禁把「到山心距離」誤當成「到界線距離」而導致吉凶相位顛倒！

6. 🚨【四靈護砂與凹風煞（Wind Shadow & Gap Venturi）】:
   - 形家砂法：後玄武（主山）宜高峻垂頭靜鎮而不逼壓；前朱雀（案朝）宜開闊、低伏、端秀有應；左青龍宜蜿蜒高起，右白虎宜馴俯低伏——「寧讓青龍高千丈，不讓白虎亂抬頭」，白虎高於青龍為凶。四勢環抱方為藏風，任一缺角即為缺衛。
   - 【物理本質】：護砂即擋風屏障（Wind Shadow），缺口即風道——兩砂／兩樓之間的缺口因狹管效應（Venturi）令氣流局部加速，直吹穴場即成「凹風煞」，吹穴大凶；城市高樓間之天斬風道與山間凹風同源同理。吉凶必須由「局部風速放大係數 + 缺口幾何相對穴位之方位」自然湧現，嚴禁用砂名／樓名標籤判定！
   - 【陰陽交媾】：孤陰不生，獨陽不長——山（陰靜）水（陽動）必須交會；有山無水、有水無山皆非結作，審計時純陽或純陰格局聚氣數據異常者應循此歸因。

7. 🚨【明堂真訣（Ming Tang Integrity）】:
   - 穴前明堂宜平整、開闊、聚窩（所謂「明堂容萬馬」），忌傾斜順坡（水直流牽牛）、忌逼窄、忌破碎、忌高壓逼迫。
   - 【物理本質】：明堂為氣之匯集緩衝區，對應觀測即穴前區域粒子減速沉降、淨流入為正、容量不超載；明堂順坡傾瀉則氣隨水走、無法駐留。

8. 🚨【地理五訣歸因框架（Dragon-Lair-Sand-Water-Facing Audit）】:
   - 正統風水以「龍、穴、砂、水、向」五訣為綱。龍：來脈宜起伏頓跌、屈曲剝換（生龍），忌直硬死蠢（死龍）——幾何上對應山脊線曲率變化豐富 vs 一瀉直線無節制。穴：太極暈層流沉降聚氣。砂：四靈環抱無缺。水：屈曲環抱有情。向：全周天連續、不出卦、不空亡。
   - 【審計鐵則】：任何 MODIFIED 判定必須能明確歸因至五訣中至少一訣的客觀數據或幾何證據；無法歸因者屬臆測，嚴禁為改而改。

🔥🔥🔥 【動態數據審查：五大物理死穴審計協議】 🔥🔥🔥
審查 Dry Run JSON 時，【絕對禁止】只看最終評級（`verdict.rating` 或 `scoringAndVerdict.verdictRating`，依數據實際欄位為準）就判定正常；同樣【絕對禁止】在未命中任何死穴時憑感覺挑毛病。逐條核對，命中任一條才准判 MODIFIED：
1. 🚨【凶煞偽聚氣 (False Gathering)】：在數據自陳的高亂流、反坡逆風或封閉死坑幾何處出現高 `gatherAcc`，且伴隨矛盾信號（如 qiDensity 極低但 gatherAcc 極高、或 sanityWarnings 含幾何/物理警告）——層流條件誤判滯留死水為結穴。（城市模若含 `yinYangBalance < -4` 可作佐證。）
2. 🚨【流體死鎖 (Zero-Activity Stall)】：`gatherAcc === 0 && scatterAcc === 0`（全域推力或平洋龍未正確給予背景場，粒子未進場）。
3. 🚨【猝死震盪 (Respawn Thrashing)】：`scatterAcc` 異常必須以相對規模判斷——scatterAcc 超過數據內粒子總數（physicsStability.particleCount）之約 25%，且伴隨至少一項輔證（maxSpeedSq 暴走、geometryAnomalies 非空、出生點鄰近擊殺邊界）方算命中。單純高 scatterAcc 而無輔證者屬「合法氣散」（大凶局本應全數渙散），嚴禁判 MODIFIED！
4. 🚨【防呆容量超載 (Capacity Violation)】：注意 capacityUtilization 為百分比「字串」且已被鉗位於 100%——超載的可觀測特徵是：明堂截斷或無護砂格局下 capacityUtilization 達 100%（飽和頂格）且 gatherAcc 持續增長。若懷疑真超載，唯一合法修法是增補未鉗位的原始比值欄位（見觀測端完整性條款），嚴禁直接放寬容量上限！
5. 🚨【時序衰竭崩潰 (Temporal Decay Collapse)】：大吉/經典格局在 20 秒以上長時間模擬中出現聚氣量隨時間單調雪崩（gatherAcc 衰退至 15 以下或散氣 scatterAcc 壓倒性超標導致評級退化為大凶）——此為粒子狀態機單向耗散或邊界誤殺之典型病竈，命中必判 MODIFIED！
【煞之分級裁決】判定死穴前必須先做空間歸因：剪切亂流源若遠離穴位、或受案山／水口／護砂阻隔衰減，屬「可化之煞」，不得僅因全域存在高亂流就判 MODIFIED；唯有直逼穴位、無遮攔的強剪切或上述死穴數據特徵才構成違規。

🔥🔥🔥 【圖學、幾何與數值穩定性鐵律】 🔥🔥🔥
1. **全無硬編碼相對尺度**：穴位周邊所有幾何、力場衰減一律基於局部相對坐標 $\Delta \vec{r} = \vec{x} - \vec{x}_{xue}$，嚴禁出現任何寫死的絕對坐標常量（如嚴禁寫 `p.z < -30`）。
2. **三維單向地面托舉與水體浮力**：陸地為單向幾何支撐 `y = Math.max(y, t.y + 0.8)`，水域垂向由浮力阻尼自然平衡，允許三維立體升降，嚴禁拍扁在單一平面。
3. **平滑過渡（Smoothstep / Gaussian Falloff）**：嚴禁階梯式硬切斷（Hard Cutoff），分母必須防禦除零（`Math.max(0.001, dist)`），嚴防 3D 破圖與 NaN 崩潰。
4. **單向數據流**：【3D地形/CFD網格】 $\rightarrow$ 【粒子動力學】 $\rightarrow$ 【Sensors物理採樣】 $\rightarrow$ 【Rules五訣評分】。評分引擎僅為客觀觀測者，嚴禁修改底層物理場。
5. **NaN/Infinity 傳染鏈防禦**：NaN 一旦進入力場，會沿【粒子動力學 → Sensors → Rules】單向數據流污染全程。`Math.acos`（參數必須夾鉗至 [-1,1]）、`Math.sqrt`（參數夾鉗至 ≥0）、`Math.atan2(0,0)`、零向量 `normalize()` 皆為 NaN 高危源；任何新增的三角／開方／歸一化運算必須自證輸入域安全或先行夾鉗。
6. **時間步長（dt）穩定性**：新增任何力項必須對幀率波動穩健——或與 dt 無關，或含阻尼／速度上界鉗位。若發現 dt 未鉗位（如切換分頁返回後 dt 暴漲導致積分爆炸、速度暴走被誤判為「氣散」）的病竈，命中即判 MODIFIED。
7. **性能預算**：每幀熱路徑嚴禁新增無空間分桶／網格加速的 O(n²) 全量掃描；嚴禁在每幀熱路徑中建立物件／陣列造成 GC 抖動；新增 geometry/material 必須確認 dispose 釋放路徑存在。物理正確但幀率崩潰同樣構成缺陷。
8. **座標與角度慣例一致性**：審計前必先核對全檔角度慣例唯一——0° 基準方向（正北？）、旋轉正向（羅盤順時針或數學逆時針）、y 軸朝向與手性。空亡線與二十四山公式所依賴的 deg 必須與羅盤方位映射自洽；若發現同一 deg 同時被當作數學極角與羅盤角混用（相位差 90° 或正負顛倒），立即判 MODIFIED。

🔥🔥🔥 【視覺畫面審查協議（僅當訊息實際附帶截圖時適用，即圖片模式）】 🔥🔥🔥
1. **【數據×畫面交叉驗證】**：截圖緊接於文字之後、依 JSON 陣列順序一一對應（數據內 `_shot` 欄位即截圖檔名）。在圖片模式下，判定應結合 JSON 數據與畫面；在純文字/未附圖模式下，本協議自動豁免，100% 依據客觀物理 JSON 數據進行判定。
2. **【畫面合理性檢查清單】**：地形網格破洞／拉花、粒子雲分佈是否與數據宣稱的聚散型態一致（高 gatherAcc 應可見明顯匯聚）、全黑／全白／NaN 黑屏、相機穿地或穿模、色彩光學異常（過曝/死黑/材質丟失）、UI 文字亂碼重疊、WebGL 錯誤提示。
3. **【畫面×數據矛盾即病竈】**：數據宣稱大吉聚氣但畫面粒子四散空場、或宣稱氣散但畫面異常堆積、或 verdictRating 與畫面直觀吉凶明顯相悖——必須在 reason 中具體描述矛盾並優先排查觀測端採樣與渲染端不同步的問題。
4. **【美學豁免】**：主觀配色、構圖角度與個人風格偏好一律不得作為 MODIFIED 依據；唯有客觀渲染缺陷（破圖、黑屏、穿模、粒子全滅、z-fighting 閃爍紋）或畫面與數據物理矛盾，才構成視覺判 MODIFIED 的合法理由。

🔥🔥🔥 【代碼修改與輸出鐵律 (CRITICAL PATCHING RULES)】 🔥🔥🔥
1. **【零註解原則 (Zero-Comment Invariance)】**：`replace` 區塊中**絕對嚴禁添加任何自創註解**（如禁止寫 `// 修正...`、`// 優化`）。代碼必須是 100% 純淨邏輯。
2. **【嚴格保留原始縮排與換行】**：`search` 與 `replace` 中的每一行，縮排與換行必須與目標檔案 100% 精確對齊。
3. **【一字不漏的 Search 區塊】**：`search` 必須取自「當前最新檔案狀態」（含先前輪次已套用的所有修改），提供 5~10 行完整上下文，嚴禁使用 `// ... (省略)`！【差分台帳】你只有在附代碼快照的輪次才能直接看到檔案；其餘輪次的當前狀態＝最近快照＋你先前輸出且系統回報「成功套用」的全部 diff，必須據此心算重建。若對當前狀態不確定，禁止猜測——將 search 錨定在最確定未被改動的區塊，並在 reason 中聲明不確定之處。
4. **【審計輸出決策】**：命中任一死穴或存在真實邏輯漏洞 → 必須回 MODIFIED；未命中任何死穴且無 sanityWarnings、無斷言失敗 → 必須回 PERFECT。【動態輪補充】動態輪資料為刻意篩選的邊界/異常案例，隨機格局攜帶 sanityWarnings 屬常態、不當然構成 MODIFIED；PERFECT 條件放寬為：五大死穴未命中，且所有警告經【煞之分級裁決】判定屬可化之煞或與格局預期一致。但經典預設格局（presetName 非 random 開頭）出現警告一律判 MODIFIED！【expectedRating 申訴權】若某經典格局的 expectedRating 聲明字串本身與正統五訣相悖（例如大凶局被錯標為上吉），允許提出修正 presetExpectations 的 diff，並在 reason 中詳述風水物理依據。【數據不足出口】若現有觀測欄位不足以支撐判定，嚴禁腦補——回 status="NEED_PROBE" 並提交觀測端增補 diff。
5. **【反震盪三護欄】**：(a) 每輪 changes 集中修復最關鍵病竈（建議 1~3 處，至多不超過 4 處原子關聯修改）；(b) 若本次修正方向與近期輪次相反，立即停止並在 reason 分析根因；(c) 嚴禁無病呻吟式修改與破壞性重構。
6. **【觀測端神聖不可侵 (Observability Integrity)】**：嚴禁為通過審計而削弱檢測靈敏度（如嚴禁註解掉警告判斷、嚴禁刪除斷言比對邏輯、嚴禁偽造綠燈數據）。觀測端改動僅限於：(1) 修復申訴成立的預期評級聲明字串；(2) 增補新觀測欄位；(3) 修復觀測端本身的計算錯誤（如修正指標採樣公式）。
7. **【批次原子性契約】**：系統對 changes 採「全有或全無」原子套用——任一片段的 search 失配，本批全部片段一併作廢退回。因此同批提交的片段必須互相獨立且全部必要；非必要的順手改動嚴禁搭車，寧可拆到後續輪次小步提交。

【輸出格式】
若發現問題需修改：
{
  "status": "MODIFIED",
  "reason": "[病徵] 引用具體數據欄位與實測值描述異常。\n[病因] 定位代碼邏輯漏洞（基於流體或幾何場論）。\n[解法] 說明具體數學與物理修正方式。",
  "changes": [
    {
      "search": "<此處放原目標代碼完整上下文，5~10 行，一字不漏>",
      "replace": "<此處放替換後的新代碼，零註解、嚴格保留原始縮排>"
    }
  ]
}

若審計完全無問題：
{
  "status": "PERFECT",
  "reason": "[複核證據] 列舉本輪實際複核的格局名稱與關鍵指標區間（gatherAcc、scatterAcc、turbulence、sanityWarnings 等實測值），證明未命中四大死穴。嚴禁照抄模板敷衍。"
}

若現有數據不足以判定：
{
  "status": "NEED_PROBE",
  "reason": "[疑點] 描述疑似異常與其數據跡象。\n[缺口] 現有欄位為何無法完成判定。\n[探針] 將增補的觀測欄位及其欲捕捉的特徵。",
  "changes": [
    {
      "search": "<原目標代碼完整上下文，5~10 行>",
      "replace": "<僅含觀測端增補的新代碼>"
    }
  ]
}
"""

# ============================================================
# 每輪尾部強制重申的終極十全緊箍咒 (封死所有物理作弊、除零崩潰與變數未定義)
# ============================================================
CORE_RULES_REMINDER = r"""
⚡⚡⚡【全域物理湧現、去硬編碼與代碼修改十七全天條（每輪強制重申・違者退回）】⚡⚡⚡
1. 【零標籤硬編碼・範圍限定】嚴禁在「新增或改寫的」動力學中使用格局名稱字串判斷（如嚴禁新寫 `state.ms === 'sunken'` 或 `state.w === 'tfork'`）！既有配置映射表（形煞清單、預設參數）屬聲明式資料，嚴禁發動大型重構剷除！吉凶必須由「曲率張量 $\nabla^2 h$」、「流場剪切亂流 $\|\nabla \times \vec{v}\|$」與「水陸邊界層阻尼」客觀決定！

2. 【界水則止真物理・得水為上】「氣乘風則散，界水則止」「得水為上，藏風次之」：緩慢環抱之水體在岸邊應自然形成邊界層減速（促成生氣沉降於太極暈）；僅在急流強剪切時才產生水煞，嚴禁將所有水體一律當成負壓排斥黑洞！彎道螺旋環流：凹岸（外彎）沖刷為反弓水大凶、凸岸（內彎）淤積為玉帶環腰大吉，吉凶由穴位與彎道曲率中心之相對幾何＋局部剪切量測湧現，嚴禁名稱標籤！水貴屈曲環抱、忌直瀉無收（水口宜關鎖迴繞）；審計權重：水法失誤重於風法失誤！

3. 【山龍平洋龍統一推力】推力由地勢梯度 $-\nabla h$ 與背景微壓差場 $\vec{v}_{drift}$ 自然合成，平洋龍（$\nabla h \approx 0$）依賴水流牽引與環境微風，嚴禁平洋地貌發生流體死鎖（gatherAcc/scatterAcc 皆為 0）！

4. 【相對坐標尺度不變性】穴周邊幾何與力場判定一律使用相對坐標 ($\Delta \vec{r} = \vec{x} - \vec{x}_{xue}$)，嚴禁寫死任何絕對坐標常量（如 `Z ≈ -30` 或絕對網格下標）！

5. 【三維立體自由度】陸地單向托舉（`y = Math.max(y, t.y + 0.8)`），水域由浮力自然平衡，嚴禁拍扁在固定高度二維薄片上！

6. 【理氣全周天連續性與空亡三級制】方位與二十四山一律使用模運算連續計算（`let deg = (rawDeg % 360 + 360) % 360`）！空亡分三級：令到山界角距 d_to_bound_shan = 7.5 - |(deg%15+15)%15 - 7.5|；令到卦界角距 d_to_bound_gua = 22.5 - |(deg%45+45)%45 - 22.5|。正針（d_to_bound_shan >= 3.0，即山心 ±4.5° 內）最吉；兼向縫針（d_to_bound_shan 介於 0°~3°）帶煞；小空亡騎 15° 山界（d_to_bound_shan -> 0）凶；大空亡騎 45° 卦界（d_to_bound_gua -> 0）出卦無氣最凶且權重最高，兩層連續場疊加取最大，嚴禁相位寫反（山心為 7.5° 最吉、界線為 0° 空亡）！

7. 【狀態機雙門檻遲滯・量綱分離】環境風速平方 windSpeedSq 與載體粒子速度平方 vel.lengthSq() 是兩個物理量，嚴禁混用，門檻比較僅限同軸！凝結(pState=2)：vel.lengthSq() < 2.0 且 turbulence < 1.5 且近地層；自由氣(pState=0/3)吹散須區分微風導引與強風吹散(windSpeedSq > 2.0~2.5)；已凝吉氣剝離遲滯：須 windSpeedSq > 3.5 或 turbulence > 3.5；紅煞(pState=1)：turbulence > 5.5。Shields 迴滯律：沉積易、再懸浮難，嚴禁倒置！窩鉗乳突四大正穴皆屬合法結穴，唯須滿足開闊微凹、層流沉降、淨流入為正之客觀條件！

8. 【平滑過渡與除零防禦】向量與距離相除必加防禦分母 (`Math.max(0.001, dist)`)，力場與地形衰減一律使用高斯或 Smoothstep，嚴禁階梯式硬切斷！

9. 【零註解與保留縮排】`replace` 區塊【絕對嚴禁添加任何自創註解】（如 `// 修正`、`// 優化`），每一行代碼必須 100% 精確保留原始縮排！

10. 【代碼 Search 100% 精確匹配】`search` 區塊必須取自「當前最新檔案狀態」，與目標檔案 100% 一字不漏精確匹配（包含縮排、空格、引號），必須提供 5~10 行完整上下文，嚴禁使用 `// ... (省略)` 偷懶！

11. 【審計輸出決策與觀測端神聖】只准輸出合法 JSON！命中死穴或真實漏洞 → MODIFIED＋diff；動態輪隨機格局經【煞之分級裁決】判定屬可化之煞者不構成 MODIFIED；未命中任何死穴 → PERFECT＋reason 列舉實際複核數據！嚴禁為了過關而削弱、註解掉 sanityWarnings 或斷言邏輯；觀測端改動僅限於增補探針欄位、修復明確的聲明基準錯漏或修正觀測指標計算公式！

12. 【反震盪三護欄】每輪 changes 至多 3 處（優先最關鍵病竈）；禁止 cosmetic 重構與無病呻吟；修正方向與近期輪次拉鋸時必須停手並在 reason 分析根因！

13. 【四靈護砂與凹風煞】玄武高鎮不逼壓、朱雀開闊低伏、青龍蜿蜒宜高、白虎馴俯宜低（寧讓青龍高千丈，不讓白虎亂抬頭）；護砂本質為擋風屏障，缺口因狹管效應（Venturi）令風局部加速直吹穴場即成凹風煞大凶，城市天斬風道同源同理；判定僅憑局部風速放大係數與缺口相對穴位幾何，嚴禁砂名樓名標籤！孤陰不生獨陽不長，純山無水、純水無山皆非結作！

14. 【明堂真訣與五訣歸因】明堂宜平整開闊聚窩，忌傾斜順坡直瀉、忌逼窄高壓；一切 MODIFIED 判定必須能歸因至「龍（屈曲剝換為生、直硬為死）、穴、砂、水、向」五訣中至少一訣之客觀數據或幾何證據，無法歸因者屬臆測，嚴禁為改而改！

15. 【數值穩定四防】NaN 傳染（acos 參數夾鉗 [-1,1]、sqrt 夾鉗 ≥0、零向量嚴禁 normalize）、dt 爆炸（新力必須幀率無關或含阻尼／速度上界鉗位，dt 未 clamp 即為病竈）、性能預算（熱路徑嚴禁無空間加速的 O(n²) 掃描與每幀配置物件）、角度慣例唯一（0° 基準與順逆時針全檔一致，空亡公式嚴禁混用數學極角與羅盤角）！

16. 【驗證契約與誠實出口】changes 全批原子套用——任一片段 search 失配整批作廢，故片段務必互相獨立且全部必要、嚴禁搭車；expectedRating 若本身違反五訣物理，可申訴並提出修正 preset 定義的 diff，嚴禁扭曲物理迎合錯誤基準；數據不足時回 NEED_PROBE＋僅觀測端增補探針（如隨機種子擷取/重放欄位），嚴禁腦補判定、嚴禁假 PERFECT！

17. 【時間演化穩定性與校準可辯護】各物理門檻允許依流體力學推導合理微調；系統必須在 20 秒以上長時間模擬中保持氣場穩定，嚴禁隨時間推移生氣自發衰減潰散！
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
                f"（當前檔案狀態＝最近快照＋其後回報『成功套用』的全部 diff；曾失敗之片段未生效）。\n"
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
            
        elif status == "NEED_PROBE":
            logger.info(f"🔬 靜態審查回報數據不足 (NEED_PROBE)，僅接受 {len(changes)} 處觀測端增補探針（不計入完美、不重置）...")
            probe_success = False
            if changes:
                probe_success = apply_code_modifications(target_file, changes, logger)
                if not probe_success:
                    logger.warning("⚠️ 探針增補套用失敗（曾失敗片段未生效），下一輪繼續靜態審查。")
                else:
                    logger.info("🔬 探針增補已套用，下一輪以新欄位繼續驗證。")
            history_logs.append({
                "round": current_round,
                "status": status,
                "reason": reason,
                "changes": changes if probe_success else []
            })
            time.sleep(2)

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
    parser.add_argument("--image", "--img", "--vision", action="store_true", dest="image_mode", help="★ 圖片視覺模式：全程純動態 (跳過靜態審查)，每輪 10 筆 Dry Run 並擷取渲染畫面截圖交給 AI 視覺判讀合理性，最後同樣進行畢業大考")
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
    IMAGE_MODE = bool(args.image_mode)
    SHOTS_DIR = f"dryrun_shots_{base_name}"       # 圖片模式截圖輸出資料夾
    MAX_ROUNDS = args.rounds
    if IMAGE_MODE:
        RUNS_PER_ROUND = max(args.runs_per_round, 10) # 圖片模式：一次 10 筆 (含畫面截圖，兼顧 Token 成本與視覺判讀品質)
    else:
        RUNS_PER_ROUND = max(args.runs_per_round, 20) # 提高平時取樣基數，加快暴露出問題
    EXAM_RUNS = 100 if is_pro else 60             # 大幅提升大考壓測量，確保抓出 1% 低機率的 Bug
    
    # ⚡ [省錢調校] 靜態審查縮短為 2 輪，避免反覆罰坐 5 輪燃燒昂貴的思考鏈 (Reasoning Tokens)
    STATIC_TARGET = 2                             # 靜態審查通過門檻 (2 輪即達標)；圖片模式自動跳過靜態
    BASE_PROGRESS = STATIC_TARGET if IMAGE_MODE else 0  # 圖片模式起始進度直接視為靜態已通過 (全程純動態)
    SUCCESS_TARGET = STATIC_TARGET + 3            # 總通關目標：2 輪靜態 + 2 輪動態 + 1 輪畢業大考 = 5 關
    
    MAX_TOKEN_THRESHOLD = 900000  # 統一拉高門檻以最大化利用 Prompt Cache 省錢
    should_reset_next = False
    
    consecutive_perfects = BASE_PROGRESS
    history_logs = []
    syntax_error_retries = 0

    conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
    needs_full_snapshot = True 

    mode_desc = f"圖片視覺模式：純動態 {RUNS_PER_ROUND} 筆/輪 + 畫面截圖判讀 -> 畢業大考" if IMAGE_MODE else f"{STATIC_TARGET}次靜態 -> 動態實測高快取模式"
    logger.info("="*60)
    logger.info(f"🚀 開始多輪對話自動化調校任務 ({mode_desc}) (目標檔案: {target_file} | 模型: {args.model})")
    if IMAGE_MODE:
        logger.info("🖼️ 圖片模式已啟用：Dry Run 後將擷取模擬器畫面交給 AI 視覺判讀。")
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
        is_static = (not IMAGE_MODE) and (consecutive_perfects < STATIC_TARGET)
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
                    f"（當前檔案狀態＝最近快照＋其後回報『成功套用』的全部 diff；曾失敗之片段未生效）。請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。\n"
                    f"{CORE_RULES_REMINDER}"
                )
                
            conversation_history.append({"role": "user", "content": user_msg})
        else:
            runs = EXAM_RUNS if is_exam else RUNS_PER_ROUND
            if is_exam:
                logger.info(f"🎓 進入【畢業大考階段】！正在啟動 {EXAM_RUNS} 次高強度高壓測試 (涵蓋所有預設案例與極端組合)...")
                
            run_results = run_browser_simulations(
                target_file, runs, logger, is_exam,
                capture_screens=IMAGE_MODE, shots_dir=SHOTS_DIR
            )
            
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
                    f"檔案已自動復原至你上一輪修改「之前」的備份版本。⚠️ 你的 search 區塊必須匹配回滾後（即修改前）的代碼，嚴禁以你剛才輸出的崩潰版本為基準。請重新檢視並提供不含語法錯誤的修正 JSON。\n"
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
                    
                consecutive_perfects = BASE_PROGRESS
                time.sleep(2)
                continue
            else:
                syntax_error_retries = 0

            # 接收 Tuple (Status, unique_results, passed_core_count)
            if not run_results or (isinstance(run_results, tuple) and run_results[0] != "SUCCESS"):
                logger.error("無法收集到 Dry Run 數據，跳過此輪。")
                consecutive_perfects = BASE_PROGRESS
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
                    f"（當前檔案狀態＝最近快照＋其後回報『成功套用』的全部 diff；曾失敗之片段未生效）。\n"
                    f"{regression_text}"
                    f"{summary_text}【高價值測試數據 (僅列出隨機邊界與異常案例)】:\n{compact_json}\n"
                    f"{CORE_RULES_REMINDER}"
                )
            
            if IMAGE_MODE:
                # 📸 圖片模式：文字請求 + 依序附加每筆樣本的渲染畫面截圖 (OpenAI Vision 多模態格式)
                shot_note = (
                    "\n【畫面截圖】緊接於本文字之後，依序附上本輪各筆樣本 Dry Run 完成後的模擬器實際渲染畫面"
                    "（張數順序與上方 JSON 陣列一致，檔名對應各筆數據的 _shot 欄位）。"
                    "請依【視覺畫面審查協議】逐張檢查渲染合理性，並與對應 JSON 數據交叉驗證。"
                )
                if CORE_RULES_REMINDER in user_msg:
                    text_out = user_msg.replace(CORE_RULES_REMINDER, shot_note + "\n" + CORE_RULES_REMINDER)
                else:
                    text_out = user_msg + shot_note
                content_parts = [{"type": "text", "text": text_out}]
                img_attached = 0
                for res in unique_results:
                    shot_name = res.get("_shot") if isinstance(res, dict) else None
                    if not shot_name:
                        continue
                    shot_file = os.path.join(SHOTS_DIR, shot_name)
                    if not os.path.exists(shot_file):
                        continue
                    try:
                        with open(shot_file, "rb") as imf:
                            b64_img = base64.b64encode(imf.read()).decode("utf-8")
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}
                        })
                        img_attached += 1
                    except Exception as img_err:
                        logger.warning(f"  ⚠️ 截圖編碼失敗 ({shot_name}): {img_err}")
                logger.info(f"  🖼️ 已附上 {img_attached}/{len(unique_results)} 張畫面截圖供 AI 視覺判讀。")
                conversation_history.append({"role": "user", "content": content_parts})
            else:
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
            consecutive_perfects = BASE_PROGRESS
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
                stage_desc = f"{SUCCESS_TARGET - STATIC_TARGET}輪動態(含畫面判讀) + 1輪畢業大考" if IMAGE_MODE else f"{STATIC_TARGET}輪靜態 + {SUCCESS_TARGET - STATIC_TARGET - 1}輪動態 + 1輪畢業大考"
                logger.info(f"🎉 通過全部 {SUCCESS_TARGET} 階段考驗 ({stage_desc})！系統已達完美穩定狀態！任務正式結束。")
                history_logs.append({"round": current_round, "status": "PERFECT", "reason": f"通過 {SUCCESS_TARGET} 階段最終大考", "changes": []})
                break
            else:
                next_is_static = (not IMAGE_MODE) and (consecutive_perfects < STATIC_TARGET)
                next_mode = "靜態審查" if next_is_static else ("畢業大考" if consecutive_perfects >= (SUCCESS_TARGET - 1) else "動態實測")
                logger.info(f"✅ 本輪判定通過 (已連續成功 {consecutive_perfects}/{SUCCESS_TARGET} 次)，準備進入第 {consecutive_perfects + 1} 階段: 【{next_mode}】...")
                time.sleep(1)
                
        elif status == "NEED_PROBE":
            logger.info(f"🔬 AI 回報數據不足 (NEED_PROBE)，僅接受 {len(changes)} 處觀測端增補探針（連續完美計數凍結：{consecutive_perfects}/{SUCCESS_TARGET}）...")
            probe_success = False
            if changes:
                probe_success, _ = try_apply_with_retries(target_file, changes, conversation_history, client, args.model, logger)
                if probe_success:
                    logger.info("🔬 探針增補已套用，下一輪以新欄位重新取樣驗證。")
                else:
                    logger.warning("⚠️ 探針增補套用失敗（曾失敗片段未生效），下一輪繼續。")
            history_logs.append({
                "round": current_round,
                "status": status,
                "reason": reason,
                "changes": changes if probe_success else []
            })
            time.sleep(2)

        elif status == "MODIFIED":
            consecutive_perfects = BASE_PROGRESS
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
            consecutive_perfects = BASE_PROGRESS

        history_logs.append({
            "round": current_round,
            "status": status,
            "reason": reason,
            "changes": changes if applied_success else []
        })

    with open(report_path, "w", encoding="utf-8") as f:
        report_title = "多輪圖片視覺模式調校報告 (Vision Dynamic Run)" if IMAGE_MODE else "多輪先靜後動調校報告 (Static then Dynamic Run)"
        f.write(f"# 3D 風水模擬器 {report_title}\n\n")
        f.write(f"- **目標檔案**：`{target_file}`\n")
        f.write(f"- **調校模型**：`{args.model}`\n")
        if IMAGE_MODE:
            f.write(f"- **運行模式**：圖片視覺模式 (純動態，每輪 {RUNS_PER_ROUND} 筆 + 畫面截圖判讀)\n")
            f.write(f"- **畫面截圖目錄**：`{SHOTS_DIR}`\n")
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