import os
import re
import json
import hashlib
from typing import List, Dict, Any
from dashscope import Generation
from core.icbc_db import ICBCVectorDB
import config.config as config
import log.logger as logger

_log = logger.get_logger()

class VoucherKnowledgeBuilder:
  def __init__(self):
    self.db = ICBCVectorDB()
    self.api_key = config.get_qwen_api_key()

  def run_full_sync(self, file_path: str):
    """一键同步流水线"""
    raw_content = self._read_file(file_path)
    if not raw_content: return

    qa_list = self._parse_to_json(raw_content)
    _log.info(f"文件解析成功，准备处理 {len(qa_list)} 条数据")

    # 调用千问批量增强
    enriched_qa_list = self._enrich_with_qwen_batch(qa_list)

    if enriched_qa_list:
      self.db.build_voucher_knowledge(enriched_qa_list)
      _log.success("立减金向量库批量重建完成")

  def _read_file(self, file_path: str) -> str:
    for enc in ['utf-8', 'gbk']:
      try:
        with open(file_path, 'r', encoding=enc) as f:
          return f.read().strip()
      except: continue
    return ""

  def _parse_to_json(self, content: str) -> List[Dict[str, str]]:
    blocks = re.split(r'\n(?=Q[:：])', content)
    results = []
    for block in blocks:
      if not block.strip(): continue
      parts = re.split(r'A[:：]', block)
      if len(parts) >= 2:
        results.append({
          "question": parts[0].replace('Q:', '').replace('Q：', '').strip(),
          "answer": parts[1].strip()
        })
    return results

  def _enrich_with_qwen_batch(self, qa_list: List[Dict[str, str]], batch_size: int = 15) -> List[Dict[str, str]]:
    all_enriched_data = []
    
    for i in range(0, len(qa_list), batch_size):
      chunk = qa_list[i : i + batch_size]
      
      system_prompt = (
        "你是一个金融数据结构化专家。我会给你一个包含序号(id)和业务内容(content)的JSON数组。\n"
        "请为每个条目生成一个'search_bundle'。该字段必须包含该知识点的核心关键词（如错误码、业务动作）和50字内的语义摘要。\n\n"
        "【输出要求】：\n"
        "1. 严禁返回原始内容(question/answer/content)。\n"
        "2. 严禁包含任何解释性文字或Markdown代码标签。\n"
        "3. 仅返回包含 id 和 search_bundle 的合法 JSON 数组。\n\n"
        "【输出范例】：\n"
        "[\n"
        "  {\n"
        "    \"id\": 0,\n"
        "    \"search_bundle\": \"关键词：兑换流程、工银微金融、小程序。摘要：通过工银微金融小程序选面额卡种并输入手机号验证即可完成立减金兑换。\"\n"
        "  },\n"
        "  {\n"
        "    \"id\": 1,\n"
        "    \"search_bundle\": \"关键词：过期处理、补发申请、客服。摘要：立减金有效期10天，首次过期可联系客服申请补发，再次过期不予处理。\"\n"
        "  }\n"
        "]"
      )
      
      # 使用 i + idx 保持全局唯一序号，方便回填
      user_input = [
        {"id": i + idx, "content": f"{item['question']} {item['answer']}"} 
        for idx, item in enumerate(chunk)
      ]

      try:
        response = Generation.call(
          model='qwen-max',
          api_key=self.api_key,
          messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': json.dumps(user_input, ensure_ascii=False)}
          ],
          result_format='message'
        )

        if response.status_code == 200:
          clean_json = re.sub(r'```json\n?|\n?```', '', response.output.choices[0].message.content).strip()
          bundles = json.loads(clean_json)
          
          # 核心改进：通过 ID 映射回填，不依赖数组顺序
          bundle_map = {item["id"]: item.get("search_bundle", "") for item in bundles}
          
          for idx, item in enumerate(chunk):
            global_idx = i + idx
            item["search_bundle"] = bundle_map.get(global_idx, "")
          
          all_enriched_data.extend(chunk)
        else:
          raise Exception(f"API Error: {response.message}")
          
      except Exception as e:
        _log.error(f"批次 {i//batch_size} 处理失败: {e}")
        # 失败兜底
        for item in chunk:
          item.setdefault("search_bundle", "")
        all_enriched_data.extend(chunk)

    return all_enriched_data

# --- 调用入口 ---
if __name__ == "__main__":
  builder = VoucherKnowledgeBuilder()
  builder.run_full_sync("materials/voucher_faq.txt")