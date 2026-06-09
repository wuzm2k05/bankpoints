import os
import re
import json
import hashlib
from typing import List, Dict, Any
from dashscope import Generation
import config.config as config
import log.logger as logger

_log = logger.get_logger()

class VoucherKnowledgeBuilder:
  def __init__(self):
    # 兼容你的底层类
    from core.icbc_db import ICBCVectorDB
    self.db = ICBCVectorDB()
    self.api_key = config.get_qwen_api_key()

  def run_full_sync(self, file_path: str):
    """一键同步流水线"""
    raw_content = self._read_file(file_path)
    if not raw_content: 
      _log.error(f"文件读取失败或内容为空: {file_path}")
      return

    qa_list = self._parse_to_json(raw_content)
    _log.info(f"文件解析成功，准备处理 {len(qa_list)} 条知识库数据")

    # 调用千问批量增强
    enriched_qa_list = self._enrich_with_qwen_batch(qa_list)

    if enriched_qa_list:
      self.db.build_voucher_knowledge(enriched_qa_list)
      _log.info("立减金向量库批量重建完成")

  def _read_file(self, file_path: str) -> str:
    for enc in ['utf-8', 'gbk']:
      try:
        with open(file_path, 'r', encoding=enc) as f:
          return f.read().strip()
      except: 
        continue
    return ""

  def _parse_to_json(self, content: str) -> List[Dict[str, str]]:
    # 完美切分带有各种换行符与中英文 Q: 头的块
    blocks = re.split(r'(?:\r?\n|^)(?=Q[:：])', content)
    results = []
    for block in blocks:
      if not block.strip(): 
        continue
      parts = re.split(r'A[:：]', block)
      if len(parts) >= 2:
        # 使用正则把开头的 Q: 或 Q：以及后面跟随的空格全部剔除干净
        q_text = re.sub(r'^Q[:：]\s*', '', parts[0]).strip()
        a_text = parts[1].strip()
        results.append({
          "question": q_text,
          "answer": a_text
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
        "  }\n"
        "]"
      )
      
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
          raw_content = response.output.choices[0].message.content
          
          # 防止大模型吐出 Markdown 标记或者多余废话，强制正则截取 JSON 数组体
          json_match = re.search(r'\[\s*\{.*\}\s*\]', raw_content, re.DOTALL)
          if not json_match:
            raise ValueError(f"未能从大模型响应中截取到合法的JSON格式。原始返回：{raw_content}")
          
          bundles = json.loads(json_match.group(0).strip())
          
          # 强制把大模型可能返回的字符串型 id 转换为 Python 整型，防止 Key 不匹配
          bundle_map = {int(item["id"]): item.get("search_bundle", "") for item in bundles}
          
          for idx, item in enumerate(chunk):
            global_idx = i + idx
            # 如果没有拿到大模型增强，优雅地降级将 Q 端的关键词作为搜索特征边界
            item["search_bundle"] = bundle_map.get(global_idx, f"关键词：{item['question']}")
          
          all_enriched_data.extend(chunk)
        else:
          # 兼容不同版本 DashScope SDK 错误信息返回结构
          err_msg = getattr(response, 'message', str(response))
          raise Exception(f"API Error Code {response.status_code}: {err_msg}")
          
      except Exception as e:
        _log.error(f"批次 {i//batch_size} 处理异常，已启用基础文本平替，错误详情: {e}")
        # 异常时给一个健全的初始默认值，防止下游向量库在索引空字段时崩溃
        for item in chunk:
          item["search_bundle"] = f"关键词：{item['question']}"
        all_enriched_data.extend(chunk)

    return all_enriched_data

# --- 调用入口 ---
if __name__ == "__main__":
  builder = VoucherKnowledgeBuilder()
  builder.run_full_sync("materials/voucher_faq.txt")