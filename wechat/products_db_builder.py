import asyncio
import time,re,json
from datetime import datetime
from typing import List, Dict, Any
from loguru import logger as _log

from wechat.talent_assitant import WechatTalentAssistant
import config.resource as resource
import core.model_factory as mf
from core.icbc_db import ICBCVectorDB

class WechatTalentDBBuilder():
  def __init__(self):
    wechat_talent = resource.get_resource()["wechat"]["talent"]
    model_name = wechat_talent.get("vector_db_model", "deepseek")
    self.db_batch_size = wechat_talent.get("vector_db_batch_sync_size",20)
    self.llm_batch_size = wechat_talent.get("vector_db_batch_llm_size",5)
    # 获取模型实例
    self.raw_llm = mf.get_model(model_name)
    self.prompt_template = wechat_talent.get("vector_db_prompt")
    self.db = ICBCVectorDB()
    
  async def batch_enhance_descriptions(self, items: List[Dict[str, Any]]) -> List[str]:
    if not items: return []
    
    # 构建prompt
    items_text = "\n".join([f"Item {i+1}: {item['title']}" for i, item in enumerate(items)])
    full_prompt = self.prompt_template.format(
        items_text=items_text, 
        item_count=len(items)
    )
    
    raw_content = "\[\]"

    try:
      # 注意，bind时参数直接传给openai sdk，所以参数名必须为stream而不是streaming
      llm_non_stream = self.raw_llm.bind(stream=False)
      response = await llm_non_stream.ainvoke(full_prompt)
      raw_content = response.content.strip()

      # 2. 核心清洗逻辑：提取 JSON 数组部分
      start_idx = raw_content.find('[')
      end_idx = raw_content.rfind(']')
      
      if start_idx != -1 and end_idx != -1:
        clean_json = raw_content[start_idx:end_idx+1]
      else:
        clean_json = raw_content

      descriptions = json.loads(clean_json)
        
      # 3. 校验数量对齐
      if isinstance(descriptions, list) and len(descriptions) == len(items):
        return [str(d).strip() for d in descriptions]
      else:
        _log.warning("LLM 返回数组长度不匹配，使用标题降级。")
        return [item['title'] for item in items]
        
    except Exception as e:
      _log.error("批量 AI 增强解析失败: {}. 原始内容: {}", e, raw_content[:100])
      return [item['title'] for item in items]

  # 2 个空格对齐
  async def sync_once(self):
    assistant = WechatTalentAssistant()
    _log.info("开始流式构建微信向量库 (支持增量复用 & 批量 AI)...")
    
    temp_name = self.db.start_rebuild_session()
    last_buffer = None
    total_count = 0
    
    # 用于存放待 LLM 增强的商品队列
    llm_queue = []
    # 用于存放最终准备写入磁盘的商品队列
    write_queue = []

    try:
      while True:
        response = assistant.get_all_products(last_buffer=last_buffer)
        products = response.get("products", [])
        last_buffer = response.get("last_buffer")

        for item in products:
          pid = str(item['product_id'])
          detail = assistant.get_detail(pid)
          if not detail: continue
          
          current_title = detail.get("title", "")
          
          # 功能 1：根据 product_id 检查旧数据库内容实现增量复用
          # 尝试从当前的“正式表”中读取旧数据
          old_data = self.db.get_product_by_id(pid)
          
          if old_data and old_data.get("title") == current_title:
            # 商品未变，直接复用 desc（即 vector db 中的 document 内容）
            _log.debug("商品 {} 未变化，复用缓存描述", pid)
            write_queue.append({
              "id": pid,
              "outId": detail.get("out_product_id"),
              "outAppId": detail.get("appid"),
              "title": current_title,
              "price": detail.get("selling_price"),
              "link": detail.get("product_promotion_link"),
              "desc": old_data.get("desc")
            })
          else:
            # 商品是新的或标题已变，加入 LLM 待处理队列
            llm_queue.append({
              "id": pid,
              "outId": detail.get("out_product_id"),
              "outAppId": detail.get("appid"),
              "title": current_title,
              "price": detail.get("selling_price"),
              "link": detail.get("product_promotion_link")
            })

          # --- 策略 A: 如果待增强队列满了，执行批量 LLM ---
          while len(llm_queue) >= self.llm_batch_size:
            # 1. 每次只取出前 llm_batch_size 个元素进行处理
            current_batch = llm_queue[:self.llm_batch_size]
            # 2. 剩余的留在原队列中
            llm_queue = llm_queue[self.llm_batch_size:]
            ai_descs = await self.batch_enhance_descriptions(current_batch)
            
            # 3. 将生成的描述回填并加入写入队列
            for i, desc in enumerate(ai_descs):
                current_batch[i]["desc"] = desc
            
            write_queue.extend(current_batch)

          # --- 策略 B: 如果待写入队列满了，分批存入临时 Collection ---
          if len(write_queue) >= 50:
            await asyncio.to_thread(self.db.append_to_rebuild_session, temp_name, write_queue)
            total_count += len(write_queue)
            write_queue = []

        if not last_buffer: break

      # 处理 llm_queue 剩余数据
      if llm_queue:
        ai_descs = await self.batch_enhance_descriptions(llm_queue)
        for i, desc in enumerate(ai_descs):
          llm_queue[i]["desc"] = desc
        write_queue.extend(llm_queue)

      # 处理 write_queue 剩余数据
      if write_queue:
        await asyncio.to_thread(self.db.append_to_rebuild_session, temp_name, write_queue)
        total_count += len(write_queue)

      if total_count > 0:
        await asyncio.to_thread(self.db.finalize_rebuild_session, temp_name)
        return True
      return False

    except Exception as e:
      _log.error("同步中断: {}", e)
      try: 
        # 先检查是否存在再删除，避免二次崩溃
        all_colls = [c.name for c in self.db.client.list_collections()]
        if temp_name in all_colls:
          self.db.client.delete_collection(temp_name)
      except: pass
      return False

async def sync_task():
  """
  周期性定时任务调度器 (内存状态版)
  """
  talent_conf = resource.get_resource()["wechat"]["talent"]
  period_days = talent_conf["sync_period_in_days"]
  check_interval_min = talent_conf["sync_check_interval_in_minutes"]
  start_hour = talent_conf["sync_start_time"]
  end_hour = talent_conf["sync_end_time"]

  last_sync_date = None
  last_sync_timestamp = 0
  builder = WechatTalentDBBuilder()

  _log.info("同步调度器启动: 每 {} 天一次, 窗口 {}:00-{}:00", 
           period_days, start_hour, end_hour)

  while True:
    try:
      now = datetime.now()
      today = now.date()
      current_hour = now.hour

      if start_hour <= current_hour < end_hour:
        days_passed = (time.time() - last_sync_timestamp) / (24 * 3600)
        already_done_today = (last_sync_date == today)

        if (days_passed >= period_days) and not already_done_today:
          _log.info("触发同步：进入时间窗口且满足周期限制")
          
          # 执行同步逻辑
          success = await builder.sync_once()
          
          if success:
            last_sync_date = today
            last_sync_timestamp = time.time()
            _log.success("本轮同步成功完成")
        else:
          _log.debug("不满足同步周期或今日已执行，跳过")
      else:
        _log.debug("不在同步时间段 (当前 {}:00)，跳过", current_hour)

    except Exception as e:
      _log.error("Sync Task 调度异常: {}", e)

    await asyncio.sleep(check_interval_min * 60)