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
    
    match = re.search(r'```(?:json)?\s*({[\s\S]*?})\s*```', text, re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
            
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        json_str = text[first_brace : last_brace + 1]
        try:
            return json.loads(json_str)
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
def run_browser_simulations(html_path, num_runs, logger):
    results = []
    abs_path = os.path.abspath(html_path)
    file_url = Path(abs_path).as_uri()
    
    logger.info(f"🌐 啟動無頭瀏覽器，準備執行 {num_runs} 次 Dry Run...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True) 
        page = browser.new_page()
        
        # 💡 [修復 1] 注入靜態腳本，屏蔽 alert 彈窗，避免阻塞 Chrome 事件循環
        page.add_init_script("window.alert = () => {};")
        
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("dialog", lambda dialog: dialog.accept())
        
        captured_json = None
        def handle_console(msg):
            nonlocal captured_json
            text = msg.text
            if "請幫我分析以下" in text or "emergenceMetrics" in text or "scores" in text:
                extracted = extract_json_from_text(text)
                if extracted:
                    captured_json = extracted
        
        page.on("console", handle_console)
        
        try:
            page.goto(file_url)
            page.wait_for_timeout(1000)
            
            if page_errors:
                logger.error(f"❌ 頁面載入失敗，發現 JavaScript 語法錯誤:\n   {page_errors[0]}")
                browser.close()
                return ("JS_ERROR", page_errors[0])

            for i in range(num_runs):
                captured_json = None
                logger.info(f"  ▶ 執行第 {i+1}/{num_runs} 次 Dry Run...")
                
                if i > 0 and i % 5 == 0:
                    page.reload()
                    page.wait_for_timeout(1000)

                # ⚡ [最大化覆蓋率] 改為每次 Dry Run 都點擊隨機生成，確保收集到 15 種完全不同的獨立格局
                page.click(".preset-btn[data-p='random']")
                page.wait_for_timeout(800)
                
                if page_errors:
                    logger.error(f"❌ 模擬過程發現 JavaScript 執行階段錯誤:\n   {page_errors[0]}")
                    browser.close()
                    return ("JS_ERROR", page_errors[0])

                # 💡 [修復 2] 加上 no_wait_after=True 與 timeout=60000
                # 避免 Playwright 因為 JS 跑 360 幀模擬過久而卡在 click action 超時
                try:
                    page.click("#btn-dry-run", no_wait_after=True, timeout=60000)
                except Exception as click_err:
                    # 備用方案：如果 Playwright click 依然失敗，使用 JS 直接觸發
                    page.evaluate("document.querySelector('#btn-dry-run')?.click()")

                # 💡 [修復 3] 延長等待 JSON 的超時時間至 15 秒（給 Headless CPU 足夠時間算完 360 幀）
                start_wait = time.time()
                while captured_json is None and (time.time() - start_wait) < 15.0:
                    if page_errors:
                        logger.error(f"❌ 模擬過程發現 JavaScript 執行階段錯誤:\n   {page_errors[0]}")
                        browser.close()
                        return ("JS_ERROR", page_errors[0])
                    page.wait_for_timeout(200)
                
                if captured_json:
                    results.append(captured_json)
                else:
                    logger.warning(f"  ⚠️ 第 {i+1} 次未捕捉到有效的 JSON 數據")
                
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

🔥🔥🔥 【特別注意：全能空間、物理與渲染合規性 (兼容雙版本)】 🔥🔥🔥
動態診斷時，請深入解析 `spatialProfile` 內的警告與數據：

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

🔥🔥🔥【特別嚴禁】🔥🔥🔥
1. 絕對禁止在 `replace` 中使用 `// ... (省略)` 或 `// 原有代碼保持不變`！你提供的 `replace` 必須是完整且可直接運行的真實代碼，否則系統會崩潰。
2. `search` 區塊至少需要 5~10 行，必須包含「完整的上下文」，絕不能只有單行代碼（否則會導致多處匹配衝突而失敗）！

如果當前測試數據完美，或經過靜態審查代碼邏輯完美無需修改，請回傳：
{
  "status": "PERFECT",
  "reason": "經過分析/靜態審查，物理引擎與代碼邏輯運作完美，各項數值與評價皆符合預期，無需修改。"
}

如果發現問題需要修改代碼，請回傳：
{
  "status": "MODIFIED",
  "reason": "簡述你發現的問題與修正邏輯...",
  "changes": [
    {
      "search": "原有的程式碼（務必完全複製當時代碼中的確切字串，包含正確縮排，至少 3-5 行，確保獨一無二）",
      "replace": "修正後的新程式碼（請保持與原代碼一致的縮排風格）"
    }
  ]
}"""

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
    # 以下為動靜交替實測循環模式 (3次動態 + 3次靜態)
    # ==========================
    is_pro = "pro" in args.model.lower()
    MAX_ROUNDS = args.rounds
    RUNS_PER_ROUND = args.runs_per_round
    EXAM_RUNS = 50 if is_pro else 25
    SUCCESS_TARGET = 6
    MAX_TOKEN_THRESHOLD = 900000  # 統一拉高門檻以最大化利用 Prompt Cache 省錢
    should_reset_next = False
    
    consecutive_perfects = 0
    history_logs = []
    syntax_error_retries = 0

    conversation_history = [{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
    needs_full_snapshot = True 

    logger.info("="*60)
    logger.info(f"🚀 開始多輪對話自動化調校任務 (動靜交替高快取模式) (目標檔案: {target_file} | 模型: {args.model})")
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
        is_static = (consecutive_perfects % 2 == 1)
        is_exam = (consecutive_perfects == 4)
        
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
                
            run_results = run_browser_simulations(target_file, runs, logger)
            
            if isinstance(run_results, tuple) and run_results[0] == "JS_ERROR":
                js_err_msg = run_results[1]
                syntax_error_retries += 1
                
                if syntax_error_retries > 3:
                    logger.error("🛑 連續 3 次語法自我修復失敗，自動終止任務並復原檔案。")
                    rollback_file(target_file, logger)
                    break
                    
                logger.warning(f"⚠️ 偵測到 JavaScript 語法崩潰 (嘗試自我修復 {syntax_error_retries}/3)...")
                rollback_file(target_file, logger) 
                
                # 💡 不 pop() 助理訊息，維持 user/assistant 角色規則並讓 AI 參考寫錯的上下文
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

            if not run_results:
                logger.error("無法收集到 Dry Run 數據，跳過此輪。")
                consecutive_perfects = 0
                time.sleep(2)
                continue

            # ⚡ [極致省錢優化] 數據去重與瘦身：移除多趟 Dry Run 中重複的 JSON 結果，並刪去無用的時間戳
            unique_results = []
            seen_hashes = set()
            for res in run_results:
                if "meta" in res:
                    del res["meta"] # 移除 timestamp 等會干擾去重的變動雜訊，因為 AI 也不需要知道測試時間
                
                # 使用排序後的 json 字串作為 Hash Key，過濾掉結構與數值完全相同的重複測試
                res_str = json.dumps(res, sort_keys=True)
                if res_str not in seen_hashes:
                    seen_hashes.add(res_str)
                    unique_results.append(res)
            
            logger.info(f"  ✂️ 數據去重壓縮：從 {len(run_results)} 筆冗餘數據，大幅瘦身至 {len(unique_results)} 筆獨立特徵 (省下海量 Token)")
            run_results = unique_results
                
            # ================= 終端機高亮顯示幾何與物理異常 =================
            for res in run_results:
                if res.get("sanityWarnings"):
                    for warn in res["sanityWarnings"]:
                        if "幾何" in warn or "物理異常" in warn or "穿模" in warn:
                            logger.error(f"📐 🚨 【幾何與物理嚴重警告】: {warn}")
                
                spatial = res.get("lairInfo", {}).get("spatialProfile", {})
                anomalies = spatial.get("geometryAnomalies", [])
                for anomaly in anomalies:
                    logger.warning(f"⛰️ 空間與物理掃描: {anomaly}")
            # ================================================================

            # ⚡ [Cache 優化] 加入 sort_keys=True 確保 JSON 結構鍵值順序 100% 決定論
            compact_json = json.dumps(run_results, separators=(',', ':'), ensure_ascii=False, sort_keys=True)
            
            if needs_full_snapshot:
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = (
                    f"```javascript\n{current_js}\n```\n\n"
                    f"【第 {current_round} 輪 - {prefix}請求】\n"
                    f"【測試數據】:\n{compact_json}"
                )
                needs_full_snapshot = False
            else:
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = f"【第 {current_round} 輪 - {prefix}請求】\n（上一輪修改已生效，這是最新的測試數據）：\n{compact_json}"
            
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
                logger.info("🎉 通過全部 6 階段考驗 (3次動態 + 3次靜態交替)！系統已達完美穩定狀態！任務正式結束。")
                history_logs.append({"round": current_round, "status": "PERFECT", "reason": "通過 6 階段最終大考", "changes": []})
                break
            else:
                next_mode = "靜態審查" if (consecutive_perfects % 2 == 1) else ("動態大考" if consecutive_perfects == 4 else "動態實測")
                logger.info(f"✅ 本輪判定通過 ({consecutive_perfects}/{SUCCESS_TARGET})，準備進入下一階段: 【{next_mode}】...")
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
        f.write(f"# 3D 風水模擬器 多輪動靜交替調校報告 (Dynamic & Static Run)\n\n")
        f.write(f"- **目標檔案**：`{target_file}`\n")
        f.write(f"- **調校模型**：`{args.model}`\n")
        f.write(f"- **總測試輪數**：{len(history_logs)}\n")
        f.write(f"- **最終狀態**：{'🎉 已達完美穩定狀態 (通過 6 階段考驗)' if consecutive_perfects >= SUCCESS_TARGET else '⚠️ 中途終止或達到最大輪數'}\n\n")
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
            
    logger.info(f"📝 已自動生成多輪動靜交替調校報告：{report_path}")

if __name__ == "__main__":
    main()