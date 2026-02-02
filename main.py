import os
import time
import asyncio
import base64
import aiohttp
import json
import re
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime, timedelta

from core import Message, Chain, log, AmiyaBotPluginInstance, bot as main_bot
from amiyabot.network.httpRequests import http_requests
from core.database.messages import MessageBaseModel, table
from peewee import CharField, IntegerField
from pydantic import BaseModel, Field

# ----------------- 插件元数据 -----------------
curr_dir = os.path.dirname(__file__)

# 模板文件路径
BULLETIN_TEMPLATE_PATH = os.path.join(curr_dir, 'template', 'bulletin.html')

IMG_SRC_PATTERN = re.compile(
    r'''(<img\b[^>]*?\bsrc\s*=\s*)(["']?)([^"'\s>]+)(\2)''',
    re.IGNORECASE,
)

BULLETIN_PREFACE_TEXT = '来自《终末地》的最新游戏公告：'

# Endfield API 端点
ENDFIELD_APIS = {
    'Windows': 'https://game-hub.hypergryph.com/bulletin/v2/aggregate?lang=zh-cn&platform=Windows&channel=1&type=1&code=endfield_5SD9TN&hideDetail=0',
    'iOS': 'https://game-hub.hypergryph.com/bulletin/v2/aggregate?lang=zh-cn&platform=iOS&channel=1&type=1&code=endfield_5SD9TN&hideDetail=0',
    'Android': 'https://game-hub.hypergryph.com/bulletin/v2/aggregate?lang=zh-cn&platform=Android&channel=1&type=1&code=endfield_5SD9TN&hideDetail=0',
}


# ----------------- 图片处理函数 -----------------
def normalize_image_src(src: str) -> Optional[str]:
    """标准化图片 URL"""
    if not src:
        return None
    normalized = src.strip()
    if not normalized:
        return None
    if normalized.startswith('data:'):
        return normalized
    if normalized.startswith('//'):
        return f'https:{normalized}'
    if normalized.startswith(('http://', 'https://')):
        return normalized
    if normalized.startswith('/'):
        return f'https://game-hub.hypergryph.com{normalized}'
    return normalized


async def fetch_image_as_data_uri(url: Optional[str], session: aiohttp.ClientSession, headers: Dict[str, str]) -> Optional[str]:
    """下载图片并转换为 data URI"""
    if not url:
        return None
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                log.warning(f"Failed to download image: {url} (status: {resp.status})")
                return None
            image_bytes = await resp.read()
            if not image_bytes:
                log.warning(f"Empty image response: {url}")
                return None
            content_type = resp.headers.get('Content-Type', 'image/png')
            encoded_data = base64.b64encode(image_bytes).decode('utf-8')
            return f'data:{content_type};base64,{encoded_data}'
    except Exception as exc:
        log.error(f"Error downloading image {url}: {exc}", exc_info=True)
    return None


async def inline_remote_images(html: str, session: aiohttp.ClientSession, headers: Dict[str, str]) -> str:
    """将 HTML 中的远程图片转换为内联 data URI"""
    if not html:
        return html

    result_parts = []
    last_index = 0

    for match in IMG_SRC_PATTERN.finditer(html):
        original_src = match.group(3)
        result_parts.append(html[last_index:match.start(3)])

        normalized_src = normalize_image_src(original_src)
        new_src = original_src

        if normalized_src and not normalized_src.startswith('data:'):
            data_uri = await fetch_image_as_data_uri(normalized_src, session, headers)
            if data_uri:
                new_src = data_uri
            else:
                new_src = normalized_src
        elif normalized_src:
            new_src = normalized_src

        result_parts.append(new_src)
        last_index = match.end(3)

    result_parts.append(html[last_index:])
    return ''.join(result_parts)


# ----------------- Pydantic 数据模型 -----------------
class BulletinItem(BaseModel):
    """公告项数据模型"""
    cid: str
    title: str
    header: str
    content: str
    start_at: int
    platform: str


# ----------------- 数据库模型 -----------------
@table
class EndfieldGroupSetting(MessageBaseModel):
    """群组推送设置表"""
    group_id: str = CharField(primary_key=True)
    bot_id: str = CharField(null=True)
    bulletin_push: int = IntegerField(default=0, null=True)


@table
class EndfieldBulletinRecord(MessageBaseModel):
    """Endfield 公告记录表"""
    bulletin_id: str = CharField(unique=True)
    platform: str = CharField()
    record_time: int = IntegerField()


# ----------------- 插件实例与生命周期 -----------------
class EndfieldBulletinPluginInstance(AmiyaBotPluginInstance):
    def install(self):
        EndfieldGroupSetting.create_table(safe=True)
        EndfieldBulletinRecord.create_table(safe=True)


bot = EndfieldBulletinPluginInstance(
    name='终末地游戏公告推送',
    version='1.0',
    plugin_id='royz-endfield-bulletin-push',
    plugin_type='',
    description='定时监听并推送终末地多平台游戏公告',
    document=f'{curr_dir}/README.md',
    instruction=f'{curr_dir}/README.md',
    global_config_schema=f'{curr_dir}/config_schema.json',
    global_config_default=f'{curr_dir}/config_default.yaml',
)


# ----------------- 状态变量 -----------------
last_check_timestamp = 0.0


# ----------------- 核心逻辑 -----------------
async def fetch_bulletin_from_platform(platform: str) -> Optional[List[BulletinItem]]:
    """从指定平台获取公告列表"""
    api_url = ENDFIELD_APIS.get(platform)
    if not api_url:
        log.error(f"未知平台: {platform}")
        return None

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }

    try:
        resp = await http_requests.get(api_url, timeout=15, headers=headers)
        if not resp or resp.response.status != 200:
            log.error(f"获取 {platform} 公告失败，状态码: {resp.response.status if resp else '无响应'}")
            return None

        data = resp.json
        if not data or 'data' not in data:
            log.error(f"{platform} 公告数据格式错误")
            return None

        bulletins = []
        bulletin_list = data.get('data', {}).get('list', [])

        for item in bulletin_list:
            try:
                html_content = item.get('data', {}).get('html', '')
                bulletin = BulletinItem(
                    cid=item.get('cid', ''),
                    title=item.get('title', ''),
                    header=item.get('header', ''),
                    content=html_content,
                    start_at=item.get('startAt', 0),
                    platform=platform
                )
                bulletins.append(bulletin)
            except Exception as e:
                log.error(f"解析公告项失败: {e}")
                continue

        return bulletins

    except Exception as exc:
        log.error(f"获取 {platform} 公告时发生异常: {exc}", exc_info=True)
        return None


async def fetch_recent_bulletins(force: bool = False) -> List[Tuple[BulletinItem, Dict[str, Any]]]:
    """
    获取最近的公告
    :param force: 是否强制获取，忽略已推送记录
    """
    max_days_back = bot.get_config('maxDaysBack', 7)
    cutoff_time = int(time.time()) - (max_days_back * 24 * 3600)

    # 根据配置获取启用的平台列表
    enabled_platforms = []
    if bot.get_config('enableWindows', True):
        enabled_platforms.append('Windows')
    if bot.get_config('enableIOS', True):
        enabled_platforms.append('iOS')
    if bot.get_config('enableAndroid', True):
        enabled_platforms.append('Android')

    bulletins_list = []

    for platform in enabled_platforms:

        bulletins = await fetch_bulletin_from_platform(platform)
        if not bulletins:
            continue

        for bulletin in bulletins:
            # 检查时间戳，忽略超过指定天数的公告
            if bulletin.start_at < cutoff_time:
                continue

            # 如果不是强制模式，检查是否已经推送过
            if not force:
                record_key = f"{bulletin.cid}_{platform}"
                if EndfieldBulletinRecord.get_or_none(bulletin_id=record_key):
                    continue
                log.info(f"发现新公告: [{platform}] {bulletin.title}")
            else:
                log.info(f"强制获取公告: [{platform}] {bulletin.title}")

            # 处理图片内联
            processed_content = bulletin.content
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                }
                async with aiohttp.ClientSession() as session:
                    processed_content = await inline_remote_images(processed_content, session, headers)
            except Exception as img_exc:
                log.error(f"处理公告图片时出错: {img_exc}", exc_info=True)

            # 准备渲染数据
            # 处理标题，移除\n字符（包括字面字符串和真实换行符）
            title_text = bulletin.title or bulletin.header
            title_text = title_text.replace('\\n', ' ').replace('\n', ' ').replace('\r', ' ')

            render_data = {
                'title': title_text,
                'platform': platform,
                'content': processed_content,
                'publish_time': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(bulletin.start_at)),
            }

            bulletins_list.append((bulletin, render_data))

    return bulletins_list


async def check_new_bulletins() -> List[Tuple[BulletinItem, Dict[str, Any]]]:
    """检查所有平台的新公告（仅返回未推送的）"""
    return await fetch_recent_bulletins(force=False)


# ----------------- 推送开关指令 -----------------
@bot.on_message(keywords=['开启终末地公告推送'], level=5)
async def enable_push(data: Message):
    if not data.is_admin:
        return Chain(data).text('抱歉Endmin，只有管理员才能设置终末地公告推送哦。')

    # 检查是否已经开启
    channel = EndfieldGroupSetting.get_or_none(
        group_id=data.channel_id, bot_id=data.instance.appid
    )

    if channel and channel.bulletin_push == 1:
        return Chain(data).text('Endmin，本群已经开启终末地公告推送了，请勿重复操作。')

    # 更新或创建记录
    if channel:
        EndfieldGroupSetting.update(bulletin_push=1).where(
            EndfieldGroupSetting.group_id == data.channel_id,
            EndfieldGroupSetting.bot_id == data.instance.appid,
        ).execute()
    else:
        EndfieldGroupSetting.create(
            group_id=data.channel_id, bot_id=data.instance.appid, bulletin_push=1
        )

    log.info(f"群组 {data.channel_id} 开启了终末地公告推送")
    return Chain(data).text('本群的终末地公告推送功能已开启！')


@bot.on_message(keywords=['关闭终末地公告推送'], level=5)
async def disable_push(data: Message):
    if not data.is_admin:
        return Chain(data).text('抱歉Endmin，只有管理员才能设置终末地公告推送哦。')

    # 更新记录
    EndfieldGroupSetting.update(bulletin_push=0).where(
        EndfieldGroupSetting.group_id == data.channel_id,
        EndfieldGroupSetting.bot_id == data.instance.appid
    ).execute()

    log.info(f"群组 {data.channel_id} 关闭了终末地公告推送")
    return Chain(data).text('指令确认，本群已关闭终末地公告推送功能。')


@bot.on_message(keywords=['查看推送平台'], level=5)
async def view_platforms(data: Message):
    """查看当前启用的推送平台"""
    enabled_platforms = []
    if bot.get_config('enableWindows', True):
        enabled_platforms.append('Windows')
    if bot.get_config('enableIOS', True):
        enabled_platforms.append('iOS')
    if bot.get_config('enableAndroid', True):
        enabled_platforms.append('Android')

    if not enabled_platforms:
        return Chain(data).text('当前没有启用任何平台的公告推送。')

    platform_text = '、'.join(enabled_platforms)
    return Chain(data).text(f'当前启用的推送平台：{platform_text}')


# ----------------- 手动触发指令 -----------------
@bot.on_message(keywords=['测试终末地公告'], level=5)
async def manual_check(data: Message):
    await data.send(Chain(data).text('正在强制获取最新的终末地公告，请稍候...'))

    # 强制获取最近的公告，忽略已推送记录
    recent_bulletins = await fetch_recent_bulletins(force=True)

    if not recent_bulletins:
        await data.send(Chain(data).text('Endmin，目前没有找到任何终末地公告。'))
        return

    # 推送所有最近的公告
    await data.send(Chain(data).text(f'找到 {len(recent_bulletins)} 条最近的公告，开始推送...'))

    for bulletin, render_data in recent_bulletins:
        await data.send(Chain(data).text(BULLETIN_PREFACE_TEXT))
        await data.send(
            Chain(data, at=False).html(
                BULLETIN_TEMPLATE_PATH,
                render_data,
                width=800,
                height=1,
            )
        )
        # 添加推送间隔
        await asyncio.sleep(2)

    await data.send(Chain(data).text(f'测试完成，已推送 {len(recent_bulletins)} 条公告。'))


# ----------------- 定时任务核心执行逻辑 -----------------
async def execute_bulletin_push():
    """执行公告推送"""
    new_bulletins = await check_new_bulletins()

    if not new_bulletins:
        return

    # 获取所有启用了推送的群组
    target_groups = list(
        EndfieldGroupSetting.select().where(EndfieldGroupSetting.bulletin_push == 1)
    )

    if not target_groups:
        log.info("发现新公告，但没有找到需要推送的群组。")
        # 即使没有群推送，也应记录下来防止下次重复检查
        for bulletin, _ in new_bulletins:
            record_key = f"{bulletin.cid}_{bulletin.platform}"
            EndfieldBulletinRecord.create(
                bulletin_id=record_key,
                platform=bulletin.platform,
                record_time=int(time.time())
            )
        return

    # 推送到所有启用的群组
    async def push_group(target_instance, channel_id, bulletin, render_data):
        try:
            await target_instance.send_message(
                Chain().text(BULLETIN_PREFACE_TEXT),
                channel_id=channel_id,
            )
            await target_instance.send_message(
                Chain().html(
                    BULLETIN_TEMPLATE_PATH,
                    render_data,
                    width=800,
                    height=1,
                ),
                channel_id=channel_id,
            )
        except Exception as e:
            log.error(f"推送到群组 {channel_id} 失败: {e}")

    # 为每条新公告推送到所有群组
    for bulletin, render_data in new_bulletins:
        push_tasks = []
        for group in target_groups:
            instance = main_bot[group.bot_id]
            if instance:
                push_tasks.append(
                    asyncio.create_task(push_group(instance, group.group_id, bulletin, render_data))
                )

        if push_tasks:
            await asyncio.wait(push_tasks)
            log.info(f"已向 {len(push_tasks)} 个群组推送公告: [{bulletin.platform}] {bulletin.title}")

        # 记录已推送的公告
        record_key = f"{bulletin.cid}_{bulletin.platform}"
        EndfieldBulletinRecord.create(
            bulletin_id=record_key,
            platform=bulletin.platform,
            record_time=int(time.time())
        )

        # 添加推送间隔，避免消息发送过快导致丢失
        await asyncio.sleep(2)


# ----------------- 定时任务调度器 -----------------
@bot.timed_task(each=60)
async def timed_check_scheduler(_):
    """定时检查新公告"""
    global last_check_timestamp

    if not bot.get_config('enablePush'):
        return

    try:
        interval_seconds = int(bot.get_config('checkInterval', 180))
    except (ValueError, TypeError):
        log.warning(f"配置中的 'checkInterval' 值无效，将使用默认值180秒。")
        interval_seconds = 180

    current_time = time.time()
    if current_time - last_check_timestamp >= interval_seconds:
        log.info(f"到达预定检查时间（间隔: {interval_seconds} 秒），准备执行公告检查。")
        last_check_timestamp = current_time
        await execute_bulletin_push()
