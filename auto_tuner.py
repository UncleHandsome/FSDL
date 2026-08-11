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
    
    fh = logging.FileHandler(log_file, encoding="utf-8")
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
    if not text:
        return None
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    
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

                if i % 2 == 1:
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
                "max_tokens": 65536
            }
            
            if "reasoner" not in model.lower() and "r1" not in model.lower():
                kwargs["response_format"] = {"type": "json_object"}

            response = client.chat.completions.create(**kwargs)
            
            usage = response.usage
            if usage:
                hit_tokens = getattr(usage, 'prompt_cache_hit_tokens', 0)
                miss_tokens = getattr(usage, 'prompt_cache_miss_tokens', 0)
                total_prompt = usage.prompt_tokens
                hit_rate = (hit_tokens / total_prompt * 100) if total_prompt > 0 else 0
                logger.info(f"  ⚡ Token 消耗: 總輸入 {total_prompt} | 快取命中: {hit_tokens} ({hit_rate:.1f}%) | 未命中: {miss_tokens}")
                
            raw_response = response.choices[0].message.content
            
            # ================= 增加：印出 AI 原始回覆與除錯日誌 =================
            logger.info(f"💬 [AI 原始回覆內容]:\n{'='*50}\n{raw_response}\n{'='*50}")
            
            extracted_json = extract_json_from_text(raw_response)
            if not extracted_json:
                logger.warning("⚠️ 警告：無法從 AI 原始回覆中解析出有效的 JSON 結構！")
            # ====================================================================

            return extracted_json, raw_response
            
        except Exception as e:
            logger.error(f"❌ API 請求失敗 (嘗試 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                sleep_time = (2 ** attempt) + random.random()
                logger.info(f"⏳ 等待 {sleep_time:.1f} 秒後重試...")
                time.sleep(sleep_time)
            else:
                return None, str(e)

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
            search_text = clean_code_block(change.get("search", "")).replace('\r\n', '\n')
            replace_text = clean_code_block(change.get("replace", "")).replace('\r\n', '\n')
            
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

            if best_ratio >= 0.75 and best_idx != -1:
                lines[best_idx : best_idx + best_w_len] = replace_text.split('\n')
                content = '\n'.join(lines)
                applied_count += 1
                logger.info(f"  ✅ [超強模糊匹配模式] 成功套用修改片段 #{idx+1} (相似度: {best_ratio*100:.1f}%)")
                continue

            logger.warning(f"  ❌ 找不到匹配的字串，無法套用修改片段 #{idx+1}！")
        
        if applied_count > 0:
            # 備份原始檔
            backup_path = html_path + ".bak"
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(original_content)
                
            # 原子寫入：先寫入 .tmp 再覆蓋，防止寫入中途崩潰導致檔案損毀
            tmp_path = html_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp_path, html_path)
            
            logger.info(f"💾 檔案已儲存更新: {html_path} (舊檔備份於 {backup_path})")
            return True
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
# 多輪靜態審查模式 (不執行瀏覽器，純 Code Review)
# ============================================================
def run_static_review(target_file, client, model, logger, report_path, max_rounds=50):
    logger.info("="*60)
    logger.info(f"🕵️‍♂️ 啟動多輪靜態代碼審查模式 (目標檔案: {target_file} | 模型: {model} | 最大輪數: {max_rounds})")
    logger.info("="*60)

    static_system_prompt = """你是一個頂尖的 3D 風水模擬器開發者與湧現物理引擎專家。
請對以下提供的 JavaScript 程式碼進行「靜態代碼審查 (Static Code Review)」。
尋找潛在的邏輯錯誤、物理引擎計算漏洞、風水規則矛盾，或 JavaScript 語法錯誤。

【注意事項】
1. 你的程式碼修改必須是「增量 (Incremental)」的。
2. 每次擷取 search 區塊時，請注意先前的對話中你已經修改過哪些程式碼，務必以「當前最新」的代碼狀態進行比對與擷取。

【輸出要求】
你必須「嚴格」輸出以下格式的 JSON，絕對不要輸出任何其他說明文字：

如果經過深入審查後，代碼邏輯完美、無安全或邏輯漏洞需要修改，請回傳：
{
  "status": "PERFECT",
  "reason": "經過靜態分析，代碼邏輯完美，無需再做任何修改。"
}

如果發現問題需要修改代碼，請回傳：
{
  "status": "MODIFIED",
  "reason": "詳細說明你發現的問題與修正邏輯...",
  "changes": [
    {
      "search": "原有的程式碼（務必完全複製『最新代碼快照』中的確切字串，請務必包含前後 2-3 行未修改的上下文作為錨點，至少 3-5 行，確保獨一無二）",
      "replace": "修正後的新程式碼（請保持與原代碼一致的縮排風格）"
    }
  ]
}"""

    conversation_history = [{"role": "system", "content": static_system_prompt}]
    history_logs = []
    needs_full_snapshot = True
    
    RESET_EVERY_N_ROUNDS = 10

    for current_round in range(1, max_rounds + 1):
        logger.info(f"\n【 靜態審查 - 第 {current_round}/{max_rounds} 輪 】")
        
        if current_round > 1 and (current_round - 1) % RESET_EVERY_N_ROUNDS == 0:
            logger.info(f"🔄 達到 {RESET_EVERY_N_ROUNDS} 輪，重啟對話快照 (確保 AI 不會失憶)...")
            conversation_history = [{"role": "system", "content": static_system_prompt}]
            needs_full_snapshot = True

        if needs_full_snapshot:
            current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
            if not current_js:
                logger.error("❌ 找不到有效的 JavaScript 代碼，靜態審查中止。")
                return
            user_msg = f"【第 {current_round} 輪靜態審查請求】\n【當前最新 JS 邏輯代碼快照】:\n```javascript\n{current_js}\n```\n請仔細審查上述代碼，找出潛在的邏輯漏洞、風水規則衝突或 JavaScript 語法錯誤。"
            needs_full_snapshot = False
        else:
            user_msg = f"【第 {current_round} 輪靜態審查請求】\n（上一輪的修改已成功套用）。\n請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。"
            
        conversation_history.append({"role": "user", "content": user_msg})
        
        ai_json, raw_text = get_ai_correction_multiturn(client, model, conversation_history, logger)
        
        if not ai_json:
            logger.error("解析 AI 回覆失敗，準備下一輪硬重置。")
            # 智慧硬重置：避免 Append-Only 爆 Context
            conversation_history = [{"role": "system", "content": static_system_prompt}]
            needs_full_snapshot = True
            time.sleep(2)
            continue
            
        conversation_history.append({"role": "assistant", "content": raw_text})
        
        status = ai_json.get("status", "UNKNOWN")
        reason = ai_json.get("reason", "無說明")
        changes = ai_json.get("changes", [])
        
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
                
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                retry_prompt = (
                    "❌ 修改套用失敗！你在上一輪提供的 search 區塊在當前檔案中找不到完全匹配的字串。\n"
                    "以下是目前檔案【最新的 JS 全貌代碼】：\n"
                    f"```javascript\n{current_js}\n```\n"
                    "請嚴格基於上述最新代碼，重新提供正確的 search 與 replace JSON。"
                )
                conversation_history.append({"role": "user", "content": retry_prompt})
                
                retry_json, retry_raw = get_ai_correction_multiturn(client, model, conversation_history, logger)
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
                conversation_history = [{"role": "system", "content": static_system_prompt}]
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
    parser.add_argument("--rounds", type=int, default=50, help="最高執行幾輪修正循環")
    parser.add_argument("--runs-per-round", type=int, default=15, help="每一輪執行幾次 Dry Run 取樣")
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
    MAX_ROUNDS = args.rounds
    RUNS_PER_ROUND = args.runs_per_round
    SUCCESS_TARGET = 6
    RESET_EVERY_N_ROUNDS = 10  
    
    consecutive_perfects = 0
    history_logs = []
    syntax_error_retries = 0

    # ⚡ 採用融合式固定 System Prompt，確保 messages[0] 前綴完全一致，達到極致 Prompt Cache Hit
    unified_system_prompt = """你是一個頂尖的 3D 風水模擬器開發者與湧現物理引擎專家。
我們的對話將維持連續歷史紀錄。你將會收到兩種類型的診斷請求：
1. 【動態 Dry Run 診斷】：分析傳入的多輪測試數據 JSON，找出物理與評分異常並提供代碼修正。
2. 【靜態代碼審查】：分析傳入的最新 JS 代碼，找出潛在的邏輯漏洞、風水規則衝突或語法錯誤並提供代碼修正。

【注意事項】
1. 你的程式碼修改必須是「增量 (Incremental)」的。
2. 每次擷取 search 區塊時，請注意先前的對話中你已經修改過哪些程式碼，務必以「當前最新」的代碼狀態進行比對與擷取。

【輸出要求】
你必須「嚴格」輸出以下格式的 JSON，絕對不要輸出任何其他說明文字：

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

    conversation_history = [{"role": "system", "content": unified_system_prompt}]
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
            
            current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
            retry_prompt = (
                "❌ 修改套用失敗！你在上一輪提供的 search 區塊在當前檔案中找不到完全匹配的字串。\n"
                "這通常是因為經過前面幾輪的修改後，代碼內容已經改變。\n"
                "以下是目前檔案【最新的 JS 全貌代碼】：\n"
                f"```javascript\n{current_js}\n```\n"
                "請嚴格基於上述最新代碼，重新提供正確的 search 與 replace JSON。"
            )
            
            conversation_history.append({"role": "user", "content": retry_prompt})
            retry_json, retry_raw = get_ai_correction_multiturn(client, model, conversation_history, logger)
            
            if not retry_json:
                break
            
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

        # 🔄 智慧硬重置：到達 10 輪強制清空歷史
        if current_round > 1 and (current_round - 1) % RESET_EVERY_N_ROUNDS == 0:
            logger.info(f"🔄 已達到 {RESET_EVERY_N_ROUNDS} 輪對話上限，正在重啟對話並準備載入最新代碼快照...")
            conversation_history = [{"role": "system", "content": unified_system_prompt}]
            needs_full_snapshot = True 

        if is_static:
            if needs_full_snapshot:
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                user_msg = (
                    f"【第 {current_round} 輪 - 靜態審查請求】\n"
                    f"【當前最新 JS 邏輯代碼快照 (請以此為基準)】:\n```javascript\n{current_js}\n```\n"
                    "請仔細審查上述代碼，找出潛在的邏輯漏洞、風水規則衝突或 JavaScript 語法錯誤。"
                )
                needs_full_snapshot = False
            else:
                user_msg = f"【第 {current_round} 輪 - 靜態審查請求】\n（上一輪修改已生效）。請繼續基於最新的代碼狀態，尋找是否還有其他潛在的問題。如果確認代碼已經完美無瑕，請回傳 PERFECT。"
                
            conversation_history.append({"role": "user", "content": user_msg})
        else:
            runs = 25 if is_exam else RUNS_PER_ROUND
            if is_exam:
                logger.info("🎓 進入【畢業大考階段】！正在啟動 25 次高強度高壓測試 (涵蓋所有預設案例與極端組合)...")
                
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
                
                if len(conversation_history) > 1 and conversation_history[-1]["role"] == "assistant":
                    conversation_history.pop()

                err_user_msg = f"【語法崩潰緊急修復】上一輪套用修改後爆發了以下 JavaScript 語法錯誤：\n```\n{js_err_msg}\n```\n檔案已自動復原至備份檔。請重新檢視並提供不含語法錯誤的修正 JSON。"
                conversation_history.append({"role": "user", "content": err_user_msg})

                fix_json, raw_text = get_ai_correction_multiturn(client, args.model, conversation_history, logger)
                
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
                
            compact_json = json.dumps(run_results, separators=(',', ':'), ensure_ascii=False)
            
            if needs_full_snapshot:
                current_js = extract_js_from_html(open(target_file, 'r', encoding='utf-8').read())
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = (
                    f"【第 {current_round} 輪 - {prefix}請求】\n"
                    f"【當前最新 JS 邏輯代碼快照 (請以此為基準)】:\n```javascript\n{current_js}\n```\n"
                    f"【測試數據】:\n{compact_json}"
                )
                needs_full_snapshot = False
            else:
                prefix = "畢業大考高壓測試" if is_exam else "Dry Run 診斷"
                user_msg = f"【第 {current_round} 輪 - {prefix}請求】\n（上一輪修改已生效，這是最新的測試數據）：\n{compact_json}"
            
            conversation_history.append({"role": "user", "content": user_msg})

        # 共通的 AI 請求與套用邏輯
        ai_json, raw_text = get_ai_correction_multiturn(client, args.model, conversation_history, logger)
        
        if not ai_json:
            logger.error("解析 AI 回覆失敗，準備硬重置對話。")
            conversation_history = [{"role": "system", "content": unified_system_prompt}]
            needs_full_snapshot = True
            consecutive_perfects = 0
            time.sleep(3)
            continue
            
        conversation_history.append({"role": "assistant", "content": raw_text})

        status = ai_json.get("status", "UNKNOWN")
        reason = ai_json.get("reason", "無說明")
        changes = ai_json.get("changes", [])
        
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
                    conversation_history = [{"role": "system", "content": unified_system_prompt}]
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