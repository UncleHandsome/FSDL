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

def load_api_key():
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key.txt")
    if not os.path.exists(key_file):
        return None
    with open(key_file, "r", encoding="utf-8") as f:
        api_key = f.read().strip()
    return api_key if api_key else None

def extract_json_from_text(text):
    if not text or not isinstance(text, str):
        return None
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if not text:
        return None
    
    match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', text, re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
            
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            pass
            
    first_bracket = text.find('[')
    last_bracket = text.rfind(']')
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        try:
            return json.loads(text[first_bracket : last_bracket + 1])
        except json.JSONDecodeError:
            pass
            
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
        browser = p.chromium.launch(headless=True) 
        page = browser.new_page()
        
        # 屏蔽 alert 彈窗，避免阻塞 Chrome 事件循環
        page.add_init_script("window.alert = () => {};")
        
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("dialog", lambda dialog: dialog.accept())
        
        try:
            page.goto(file_url)
            page.wait_for_timeout(1000)
            
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
            })();
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
                })();
                """
                raw_results = page.evaluate(exam_script)
                if raw_results:
                    if not isinstance(raw_results, list): raw_results = [raw_results]
                    for res in raw_results:
                        preset_name = res.get("presetName", "")
                        is_fixed = preset_name and not preset_name.startswith("random")
                        has_warnings = len(res.get("sanityWarnings", [])) > 0
                        expected = res.get("expectedRating", "動態判定")
                        actual = res.get("verdict", {}).get("rating", "")
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
                        page.reload()
                        page.wait_for_timeout(1000)
                        if page_errors: return ("JS_ERROR", page_errors[0])

                    page.click(f".preset-btn[data-p='{preset}']")
                    page.wait_for_timeout(1500)
                    if page_errors: return ("JS_ERROR", page_errors[0])

                    res = page.evaluate(dry_run_script)
                    if not res: continue

                    has_warnings = len(res.get("sanityWarnings", [])) > 0
                    expected = res.get("expectedRating", "動態判定")
                    actual = res.get("verdict", {}).get("rating", "")
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
                        page.reload()
                        page.wait_for_timeout(1000)
                        if page_errors: return ("JS_ERROR", page_errors[0])

                    page.click(".preset-btn[data-p='random']")
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
# 多輪對話 AI 診斷 (指數退避重試 + 極速快取)
# ============================================================
def get_ai_correction_multiturn(client, model, conversation_history, logger):
    logger.info(f"🧠 正在請求 AI 模型 ({model}) 進行診斷 (對話歷史深度: {len(conversation_history)})...")
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": model,
                "messages": conversation_history,
                "temperature": 0.1,
                "max_tokens": 16384
            }
            
            # ⚡ [修復空回覆 Bug] 移除 API 層級的強制 JSON 模式 (response_format)
            # 當上下文中(如測試數據)充滿大量 JSON 時，強制 JSON 模式極易觸發模型產生幻覺，
            # 以為 JSON 已經結束而直接輸出停止詞 (EOS Token)，導致 100% 空回覆。
            # 我們已在 System Prompt 要求輸出 JSON，且有強大的 Regex 擷取函數，故無需此參數。

            response = client.chat.completions.create(**kwargs)
            
            usage = response.usage
            if usage:
                hit_tokens = getattr(usage, 'prompt_cache_hit_tokens', 0)
                miss_tokens = getattr(usage, 'prompt_cache_miss_tokens', 0)
                total_prompt = usage.prompt_tokens
                hit_rate = (hit_tokens / total_prompt * 100) if total_prompt > 0 else 0
                logger.info(f"  ⚡ Token 消耗: 總輸入 {total_prompt} | 快取命中: {hit_tokens} ({hit_rate:.1f}%) | 未命中: {miss_tokens}")
                
            raw_response = response.choices[0].message.content or ""
            
            # ================= 增加：印出 AI 原始回覆與除錯日誌 =================
            logger.info(f"💬 [AI 原始回覆內容]:\n{'='*50}\n{raw_response}\n{'='*50}")
            
            extracted_json = extract_json_from_text(raw_response)
            if not extracted_json:
                logger.warning(f"⚠️ 警告：無法從 AI 原始回覆中解析出有效的 JSON 結構！(嘗試 {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) + random.random()
                    logger.info(f"⏳ 等待 {sleep_time:.1f} 秒後自動重試 API 請求...")
                    time.sleep(sleep_time)
                    continue
            # ====================================================================
            
            # ⚡ [省錢優化] 拔除冗長的 <think> 思考過程，不塞入歷史紀錄，避免 Context 爆炸與無謂 Input 計費
            history_text = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()

            prompt_tokens = usage.prompt_tokens if usage else 0
            return extracted_json, history_text, prompt_tokens
            
        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"❌ API 請求失敗 (嘗試 {attempt+1}/{max_retries}): {e}")
            
            # ⚡ [防閃退保護] 若觸發 Context Token 上限，強制回傳特殊狀態，讓主循環執行硬重置清空記憶
            if "context_length_exceeded" in error_msg or "context length" in error_msg or "too large" in error_msg:
                logger.error("🚨 偵測到 Token 歷史爆量！準備緊急觸發硬重置...")
                return {"status": "CONTEXT_LIMIT", "reason": "歷史 Token 爆量，觸發緊急重置"}, error_msg, 0

            if attempt < max_retries - 1:
                sleep_time = (2 ** attempt) + random.random()
                logger.info(f"⏳ 等待 {sleep_time:.1f} 秒後重試...")
                time.sleep(sleep_time)
            else:
                return None, str(e), 0
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
UNIFIED_SYSTEM_PROMPT = """你是一個頂尖的 3D 風水模擬器開發者、WebGL/Three.js 幾何專家與湧現物理引擎專家。
我們的對話將維持連續歷史紀錄。你將會收到兩種類型的診斷請求：
1. 【動態 Dry Run 診斷】：分析傳入的多輪測試數據 JSON。
2. 【靜態代碼審查】：分析傳入的最新 JS 代碼。

🔥🔥🔥 【數據欄位物理期望與除錯字典 (Metric Benchmarks & Ground Truth)】 🔥🔥🔥
當你審查 JSON 數據與程式碼時，請嚴格對照以下「物理真相基準」。若發現矛盾，請直接定位對應的函式進行修正：

1. **windSpeed (穴心風速)**：
   - 【期望】：無風設定或吉局時應維持低檔 (< 1.5)。
   - 【除錯】：若異常飆高，代表 `Physics.update` 誤將「粒子自主前進的流速」算成了風煞！請改用 CFD 網格風速 (`windGridX`/`windGridZ`) 作為感測來源。

2. **waterDrag (水力抽吸/伯努利負壓)**：
   - 【期望】：僅在凶水（割腳、反弓、直沖等）且靠近水邊時才應有顯著數值 (> 0.5)。玉帶水/九曲水應極低。
   - 【除錯】：若吉局出現高水力抽吸，通常是感測器的「距離判定」或「相對座標 (`dzTaiji`)」寫錯，導致明堂被誤判為深水區。

3. **gatherRatio / scatterAcc (聚氣/散氣率)**：
   - 【期望】：吉局運作 3 秒後，聚氣率應 ≥ 60%，且 `scatterAcc` 極低。大凶局（天斬、地絕）則相反。
   - 【除錯】：若吉局散氣爆量，代表 `isEscaping` 誤判，或是太極暈/明堂的引力場 (`attractForce`) 計算失效，請檢查 `Physics.update` 中的邊界與阻尼邏輯。

4. **Capacity / capLimit (明堂極限容量 c)**：
   - 【期望】：吉局（有廣場/明堂）應 > 100；凶局或明堂破敗（如 sunken, none）應嚴格受限 (< 40 或 < 60)。
   - 【除錯】：若明堂破敗卻有 150 滿容量，代表 `Rules.getCapacityLimit` 漏掉了防呆保護。

5. **yinYangBalance (陰陽平衡)**：
   - 【期望】：吉局應落在 -3 ~ +3 之間（平）；曠野氣散(yang_extreme)應 > 4；陰濕逼壓(yin_extreme)應 < -4。
   - 【除錯】：若完美格局顯示「陽氣過盛」，代表天空可視率 (`skyViewFactor` / `frontOpenness`) 或風速算錯，導致系統誤判為空曠。

6. **Z-Fighting 與 拓撲奇異點 (Tearing/破圖)**：
   - 【期望】：`sanityWarnings` 中不應出現地形撕裂或 Z-Fighting。
   - 【除錯】：⚠️ 絕對不要去改 Three.js 的 Material、DepthTest 或 Renderer！破圖 100% 是因為 `buildTerrain` 裡面的高度陣列 (`hMap`) 發生數值斷層（如 `Math.max` 與 `Math.min` 邏輯寫反、`riverRefZ` 深度設定高於地表）。請修正高度數學公式。

7. **Mountain (hMap) vs City (blds) 引擎架構區分**：
   - 山林版 (`3D.html`) 的地形依賴 1D 高度陣列 `hMap[z * size + x]`。
   - 城市版 (`city.html`) 依賴 AABB 碰撞方塊 `builder.blds` (`b.x, b.z, b.w, b.d`)。
   - 【除錯】：修改代碼時請認明你正在修改哪個檔案，絕不可混用兩者的碰撞與地形讀取邏輯。

🔥🔥🔥 【特別注意：全能空間、物理與渲染合規性 (兼容雙版本)】 🔥🔥🔥
動態診斷時，請深入解析 JSON 內的警告與數據：

1. **解讀 11x11 降維微縮地圖與八方雷達 (Topo & Radar)**：
   - 觀察 `topoMatrix_11x11` 與 `skylineRadar` 是否出現斷崖錯位、或左右極度不對稱（如 W=45, E=15）。
   - 💡 常見原因：誤用絕對座標 (`z` 而非 `dzLair`) 導致穴星位移時破圖；或是邊界鉗制寫反 (`Math.min` 誤用為 `Math.max`) 導致山脈被強行向內擠壓。

2. **城市建築穿模與重疊 (City Blds Overlap)**：
   - 若異常陣列中出現【建築穿模警告】，代表 `app.builder.blds` 中生成的方塊幾何 (x, z, w, d) 發生了不合理的重疊（例如青龍和白虎擠在一起）。請調整生成坐標或寬度。

3. **系統熵值、高維物理與流體 (Thermodynamics & CFD)**：
   - 【山林版 (3D.html)】：若出現【動能暴走】或【碰撞衰減(穿模)】，代表 `Physics.update` 中的引力或法線推力發生除以零。請增加阻尼 (`velocity.multiplyScalar`)。
   - 【城市版 (city.html)】：若 `entropyGenRate` 異常飆高或 SVF (`skyViewFactor`) 不合理，代表 `FengShuiEngine.analyze` 出現無效邊界，導致熱力學失控。
   - ⚡【全新高維指標】：請嚴格關注 JSON 中 `thermodynamicsAndTopology` 節點。
     * **Shannon Entropy (香農熵)**：若熵值過高 (通常 >5.0 代表混亂)，代表氣場未聚。
     * **Betti-1 Closure (拓撲閉合度)**：若閉合度過低 (<50%)，代表盆地漏風或缺砂。
     * **Resonant Q-Factor (駐波共振)**：觀察是否有效發揮太極暈聚氣。
     * **Reynolds & Froude (流體力學)**：判斷是否產生超臨界流煞氣 (Fr > 1.0) 或極端湍流 (Re > 4000)。
     * **Fractal Dimension (分形維度)**：觀察大環境能量級聯是否連續。
     請將這些數據的異常納入修正判斷，若發現矛盾（如：評分給予完美，但香農熵極高或閉合度極低），請務必修改程式碼以修復計算邏輯。

4. **防呆狀態悖論 (State Desync)**：
   - 若出現不可能的組合 (如平洋龍 + 高山懸崖，或城市無水卻有三叉水)，請檢查 `UI` 層的 `rules` 或相關防呆判斷是否遺漏。

【注意事項】
1. 你的程式碼修改必須是「增量 (Incremental)」的。
2. 每次擷取 search 區塊時，請務必以「當前最新」的代碼狀態進行比對與擷取。

【輸出要求】
你必須「嚴格」輸出以下格式的 JSON，絕對不要輸出任何其他說明文字。

🔥🔥🔥 【風水引擎數學與座標系鐵律 (Engine Math & Coordinate Laws)】 🔥🔥🔥
1. **相對座標 vs 絕對座標**：
   - 穴位是可以被玩家「手動點穴」或「高山點穴」移動的！
   - ❌ 錯誤寫法：`if (z > 20 && z < 50)` (這會導致穴位移動後，物理判定區留在原地，引發狀態矛盾)。
   - ✅ 正確寫法：使用 `dzTaiji` (Z軸距穴心距離) 或 `dxTaiji`，例如 `if (dzTaiji > 15 && dzTaiji < 45)`。

2. **禁止作弊 (No Hardcoding Scores)**：
   - ❌ 錯誤寫法：`if (st.presetName === 'perfect') g = 100;` 
   - ✅ 系統要的是「自然湧現」。如果你發現 perfect 局分數太低，你必須去查「是哪個地形公式擋住了氣流」、「是哪個形煞被誤判」，然後去修復那個 **物理公式**，絕不允許直接竄改最終分數！

3. **防退化原則 (Regression Prevention)**：
   - 當你要修復某個特定煞氣（如天斬煞）的 Bug 時，請將修改限縮在 `if (state.sha.includes('tianzhan'))` 區塊內。
   - ⚠️ 絕對不要隨意更改全域的阻尼係數 `Math.pow(0.98, dt)` 或全域重力，那會導致原本正常的其他 14 個格局瞬間崩潰！

🔥🔥🔥 【圖學與數值穩定性鐵律 (Graphics & Numerical Stability)】 🔥🔥🔥
1. **光學過曝與通用色彩異常 (Optical & Color Anomalies)**：
   - 若 JSON 回報 `【光學異常】被單一異常色彩覆蓋` 或 `過曝泛白` 或 `全黑`，代表 WebGL 渲染層發生了災難性錯誤。
   - 【可能病因與解法】：
     1. **熱力圖或粒子透明度疊加失控**：調降 `rgba` 中的 Alpha 值、減少自發光倍率 (`totalEmissiveRadiance += heatColor.rgb * 0.4`)。
     2. **相機掉入地底 / 巨型穿模**：檢查相機的 `safeMinY` 碰撞邏輯，或是地形高度生成是否出現 `NaN` 或無限大。
     3. **Shader 計算錯誤**：檢查 `terrainMat.onBeforeCompile` 中的 GLSL 代碼是否有除以零、未賦值變數，導致整片材質崩潰成單一顏色。
     4. **顏色變數寫錯**：檢查 `this.pCol` 初始化是否給予了不合理的顏色，或是 `color.setHex()` 溢出。

2. **平滑過渡 (Smooth Falloff) 絕對優先**：
   - 在修改地形 `y` 高度或流體速度 `vel` 時，絕對禁止使用「硬切斷 (Hard Cutoff)」。硬切斷會導致 3D 網格法線斷裂與嚴重的 Z-Fighting 破圖。
   - ❌ 致命錯誤：`if (dist < 15) { y -= 10; }` (邊緣會產生 90 度垂直斷崖)。
   - ✅ 正確做法：使用高斯衰減 (Gaussian Falloff) 或線性插值。例如 `y -= 10 * Math.exp(-(dist*dist)/50);`，讓地形與力場平滑過渡。。

2. **除以零防禦 (Zero-Division & NaN Prevention)**：
   - 任何涉及距離 `dist`、長度 `length`、或向量相除的公式，分母絕對不可為 0。當你在 JSON 看到 `CRITICAL: 數值出現 NaN`，99% 是因為除以零！
   - ❌ 致命錯誤：`let force = 10 / dist;` 或 `let dirX = dx / dist;` (當粒子恰好在中心點時，dist 為 0 導致 NaN 爆炸)。
   - ✅ 正確做法：`let force = 10 / Math.max(0.001, dist);` 永遠為分母加上最小安全閾值 (Epsilon)。

3. **向量歸一化防暴走 (Vector Normalization)**：
   - 在賦予粒子速度 (`vel.x += ...`) 時，如果牽涉到方向，務必確保方向向量已被歸一化 (Normalized)，否則距離越遠的地方，引力/斥力會無限放大導致「動能暴走」。
   - ❌ 致命錯誤：`vel.x += dx * speed * dt;` (dx 可能高達 100，導致速度瞬間破表)。
   - ✅ 正確做法：`vel.x += (dx / Math.max(0.001, dist)) * speed * dt;`。

4. **乘數懲罰的疊加防呆 (Multiplier Stacking Prevention)**：
   - 扣分或懲罰時，盡量使用 `Math.max()` 設置下限，避免多個形煞同時存在時，連乘導致分數變負數或趨近於無限小。
   - ❌ 錯誤寫法：`g -= 50;` (可能導致 g 變成負數，引發後續計算崩潰)。
   - ✅ 正確寫法：`g = Math.max(0, g - 50);` 永遠保護物理指標的安全底線。

🔥🔥🔥【代碼修改與輸出鐵律 (CRITICAL PATCHING RULES)】🔥🔥🔥
你必須「嚴格」輸出以下格式的 JSON，絕對不要在 JSON 外輸出任何 markdown 說明文字。

1. **一字不漏的 Search 區塊**：
   - `search` 區塊的內容，必須與原始代碼 **100% 完全一致**（包含縮排、空格、引號類型、註解）。
   - ❌ 嚴禁在 `search` 或 `replace` 中使用 `// ... (省略)` 或是 `// 原有代碼保持不變`！你提供的 `replace` 必須是完整可運行的真實代碼。
   - `search` 區塊**至少需要提供 5~10 行**的完整上下文，絕不能只有單行代碼，否則 Python 腳本會因為「找到太多重複的行」而拒絕套用！

2. **Reason 欄位的結構化思考**：
   - 在 JSON 的 `reason` 欄位中，請按照以下三步簡述你的思考邏輯：
     1. [病徵]：JSON 測試數據中哪裡不合理（例如：perfect 局的 scatterAcc 達 80）。
     2. [病因]：定位到哪行代碼的數學邏輯導致此現象。
     3. [解法]：你修改了什麼參數來解決它。

【輸出範例】
如果當前測試數據 `sanityWarnings` 為空，且物理指標完全符合期望，請回傳：
{
  "status": "PERFECT",
  "reason": "經過嚴格分析，物理引擎運作完美，無任何拓撲/穿模/數值異常，無需修改。"
}

如果發現問題需要修改代碼，請回傳：
{
  "status": "MODIFIED",
  "reason": "[病徵] diwang 局水力抽吸異常高。\n[病因] 淋頭水判定誤用了絕對座標 z > 0。\n[解法] 已將判定改為相對座標 dzTaiji，並加入 isLintou 狀態保護。",
  "changes": [
    {
      "search": "                if (isNearWater && isBadWaterSensor && z > waterSenseZStart && z < waterSenseZEnd && Math.abs(x) < 50) {\n                    waterDragCount++;",
      "replace": "                // 【修正】確保只有真正的凶水且在有效範圍內才引發抽吸，避免誤傷吉水\n                if (isNearWater && isBadWaterSensor && dzTaiji > 5 && dzTaiji < 45 && Math.abs(dxTaiji) < 50) {\n                    waterDragCount++;"
    }
  ]
}
"""

# ============================================================
# 多輪靜態審查模式 (不執行瀏覽器，純 Code Review)
# ============================================================
def run_static_review(target_file, client, model, logger, report_path, max_rounds=125):
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
            user_msg = f"```javascript\n{current_js}\n```\n\n【第 {current_round} 輪靜態審查請求】\n請仔細審查上述 JS 邏輯代碼快照，找出潛在的邏輯漏洞、風水規則衝突或 JavaScript 語法錯誤。"
            needs_full_snapshot = False
        else:
            user_msg = f"【第 {current_round} 輪靜態審查請求】\n（上一輪的修改已成功套用）。\n請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。"
            
        conversation_history.append({"role": "user", "content": user_msg})
        
        # 共通的 AI 請求與套用邏輯
        ai_json, raw_text, prompt_tokens = get_ai_correction_multiturn(client, args.model, conversation_history, logger)
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
                    "⚠️ 常見錯誤原因：你可能自己縮寫或省略了原始代碼中的某些參數（例如顏色的 Hex 碼）。\n"
                    "請『一字不漏』地複製當前最新代碼作為 search，並擴大上下文行數，重新提供正確的 JSON。"
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
    parser.add_argument("--file", type=str, default="3D.html", help="HTML 檔案路徑")
    parser.add_argument("--rounds", type=int, default=125, help="最高執行幾輪修正循環")
    parser.add_argument("--runs-per-round", type=int, default=10, help="每一輪執行幾次 Dry Run 取樣")
    parser.add_argument("--model", type=str, default="deepseek-v4-flash", help="API 模型名稱")
    parser.add_argument("--static", action="store_true", help="啟用靜態代碼審查模式 (不執行瀏覽器，僅循環審查代碼)")
    args = parser.parse_args()

    base_name = os.path.splitext(os.path.basename(args.file))[0]
    log_file = f"auto_tuner_{base_name}.log"
    report_path = f"tuning_report_{base_name}.md"

    logger = setup_logger(log_file)
    
    api_key = load_api_key()
    if not api_key:
        logger.error("❌ 找不到 api_key.txt 或 Key 為空！")
        return
        
    target_file = args.file
    if not os.path.exists(target_file):
        logger.error(f"❌ 找不到目標檔案: {target_file}")
        return

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    if args.static:
        run_static_review(target_file, client, args.model, logger, report_path, args.rounds)
        return

    # ==========================
    # 以下為先靜後動實測循環模式 (連續5次靜態完美 -> 3次動態大考)
    # ==========================
    is_pro = "pro" in args.model.lower()
    MAX_ROUNDS = args.rounds
    RUNS_PER_ROUND = max(args.runs_per_round, 20) # 提高平時取樣基數，加快暴露出問題
    EXAM_RUNS = 100 if is_pro else 60             # 大幅提升大考壓測量，確保抓出 1% 低機率的 Bug
    SUCCESS_TARGET = 8                            # 延長考驗階段至 8 關，杜絕幸運過關
    MAX_TOKEN_THRESHOLD = 900000  # 統一拉高門檻以最大化利用 Prompt Cache 省錢
    should_reset_next = False
    
    consecutive_perfects = 0
    history_logs = []
    syntax_error_retries = 0

    conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
    needs_full_snapshot = True 

    logger.info("="*60)
    logger.info(f"🚀 開始多輪對話自動化調校任務 (5次靜態 -> 動態實測高快取模式) (目標檔案: {target_file} | 模型: {args.model})")
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
                "1. 你省略了原本代碼中的某些參數（例如顏色的 Hex 碼 `0x16203a` 等），導致字串比對失敗。\n"
                "2. 經過前面的修改，目標代碼已經長得不一樣了。\n"
                "請『一字不漏』地複製當前最新代碼作為 search，並擴大上下文行數，重新提供正確的 JSON。"
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
        # ⚡ [策略優化] 靜態分析要連續五輪都找不到問題，才進入動態實測
        # 只要發生修改，consecutive_perfects 歸零，就會重新計算 5 輪靜態審查
        is_static = (consecutive_perfects < 5)
        is_exam = (not is_static) and (consecutive_perfects >= 6) # 第 6, 7 關升級為大考
        
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
                    "請仔細審查上述 JS 邏輯代碼快照，找出潛在的邏輯漏洞、風水規則衝突或 JavaScript 語法錯誤。"
                )
                needs_full_snapshot = False
            else:
                user_msg = f"【第 {current_round} 輪 - 靜態審查請求】\n（上一輪修改已生效）。請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。"
                
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
                
                err_user_msg = f"【語法崩潰緊急修復】上一輪套用修改後爆發了以下 JavaScript 語法錯誤：\n```\n{js_err_msg}\n```\n檔案已自動復原至備份檔。請重新檢視並提供不含語法錯誤的修正 JSON。"
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

            # ⚡ [Cache 優化] 加入 sort_keys=True 確保 JSON 結構鍵值順序 100% 決定論
            compact_json = json.dumps(unique_results, separators=(',', ':'), ensure_ascii=False, sort_keys=True)
            
            summary_text = f"✅ 已在背景默默通過 {passed_core_count} 項經典防迴歸測試，未發現異常（已隱藏其詳細 JSON 以節省 Token 空間）。\n\n" if passed_core_count > 0 else ""

            if needs_full_snapshot:
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = (
                    f"```javascript\n{current_js}\n```\n\n"
                    f"【第 {current_round} 輪 - {prefix}請求】\n"
                    f"{summary_text}【高價值測試數據 (僅列出隨機邊界與異常案例)】:\n{compact_json}"
                )
                needs_full_snapshot = False
            else:
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = f"【第 {current_round} 輪 - {prefix}請求】\n（上一輪修改已生效）。\n{summary_text}【高價值測試數據 (僅列出隨機邊界與異常案例)】:\n{compact_json}"
            
            conversation_history.append({"role": "user", "content": user_msg})

        # 共通的 AI 請求與套用邏輯
        ai_json, raw_text, prompt_tokens = get_ai_correction_multiturn(client, args.model, conversation_history, logger)
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
                logger.info(f"🎉 通過全部 {SUCCESS_TARGET} 階段考驗 (5輪靜態 + 3輪動態)！系統已達完美穩定狀態！任務正式結束。")
                history_logs.append({"round": current_round, "status": "PERFECT", "reason": f"通過 {SUCCESS_TARGET} 階段最終大考", "changes": []})
                break
            else:
                next_is_static = (consecutive_perfects < 5)
                next_mode = "靜態審查" if next_is_static else ("畢業大考" if consecutive_perfects >= 6 else "動態實測")
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
        f.write(f"- **最終狀態**：{'🎉 已達完美穩定狀態 (通過 8 階段考驗)' if consecutive_perfects >= SUCCESS_TARGET else '⚠️ 中途終止或達到最大輪數'}\n\n")
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