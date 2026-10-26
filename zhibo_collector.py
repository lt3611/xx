#!/usr/bin/env python3
"""
TVBox 直播源自动采集与测速
功能：
  1. 从 tvbox.clbug.com 获取源列表
  2. 并发测速筛选可用的 JSON 源
  3. 提取所有源中的 lives (直播列表)
  4. 对直播流进行并发测速，剔除失效源
  5. 按分组去重，生成标准 zhibo.txt
"""
import json, sys, re, subprocess, os, time
import concurrent.futures
import urllib.parse
from urllib.parse import urljoin, urlparse

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(WORK_DIR, "zhibo.txt")

# ── 基础工具函数 ──

def curl_cmd(url, timeout=10, extra_args=None):
    """执行 curl 命令"""
    cmd = ["curl", "-s", "-L", "--connect-timeout", str(timeout), "--max-time", str(timeout*2), "-A", "Mozilla/5.0"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout*2+5)
        return r.stdout.decode("utf-8", errors="replace")
    except:
        return ""

def parse_json(raw):
    """解析 JSON，支持容错"""
    if not raw: return None
    raw = raw.lstrip('﻿'); raw = re.sub(r',(\s*[}\]])', r'\1', raw)
    try: return json.loads(raw, strict=False)
    except:
        s, e = raw.find('{'), raw.rfind('}')
        if s >= 0 and e > s:
            try: return json.loads(raw[s:e+1], strict=False)
            except: pass
    return None

def check_url_speed(url, timeout=5):
    """检查 JSON 源是否存活并返回延迟(ms)"""
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code},%{time_total}",
           "--connect-timeout", str(timeout), "--max-time", str(timeout*2),
           "-L", "-A", "Mozilla/5.0", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout*2+2)
        res = r.stdout.decode().strip().split(",")
        if len(res) == 2 and res[0].startswith("2"):
            return int(float(res[1]) * 1000)
    except: pass
    return 99999

def test_live_stream(live_item):
    """
    测试单条直播流是否存活
    通过读取前 2KB 数据判断 (m3u8 会返回文本，flv/ts 会返回二进制)
    """
    url = live_item.get("url", "")
    if not url.startswith("http"): return None
    
    # 测速并读取 2KB
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code},%{time_total}",
           "-r", "0-2048", "--connect-timeout", "4", "--max-time", "8", "-A", "Mozilla/5.0", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        res = r.stdout.decode().strip().split(",")
        if len(res) == 2 and res[0].startswith("2"):
            lat = int(float(res[1]) * 1000)
            # 延迟太高(>5秒)直接丢弃
            if lat < 5000:
                return {**live_item, "_lat": lat}
    except: pass
    return None

# ── 主流程 ──

def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] 开始采集直播源...")

    # ── 1. 获取源列表 ──
    print(">>> 获取源列表...")
    html = curl_cmd("https://tvbox.clbug.com/user.php", timeout=15)
    src_urls = re.findall(r'data-url="([^"]+)"', html)
    src_names = re.findall(r'<td class="td-name">([^<]+)</td>', html)
    sources = [(n.strip(), u.strip().replace("&amp;", "&"))
               for n, u in zip(src_names, src_urls)
               if u.strip() and not u.strip().startswith("#")]
    print(f"    找到 {len(sources)} 个源")

    # ── 2. 并发测速筛选 JSON 源 ──
    print(">>> 并发测试 JSON 源连通性...")
    available = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        future_to_src = {pool.submit(check_url_speed, url): (name, url) for name, url in sources}
        for future in concurrent.futures.as_completed(future_to_src):
            name, url = future_to_src[future]
            lat = future.result()
            if lat < 99999:
                available.append((name, url, lat))
    available.sort(key=lambda x: x[2])
    print(f"    存活 JSON 源: {len(available)} 个")

    # ── 3. 提取所有直播列表 ──
    print(">>> 解析 JSON 提取直播列表...")
    all_lives = {}  # 以 url 为键去重
    
    for name, url, lat in available:
        data = parse_json(curl_cmd(url, timeout=10))
        if not data: continue
        
        for l in (data.get("lives") or []):
            u = l.get("url", "").strip()
            if u and u.startswith("http") and u not in all_lives:
                # 提取分组，兼容不同字段名
                group = l.get("group") or l.get("group-title") or "未分组"
                clean_name = l.get("name", "未知频道")
                
                all_lives[u] = {
                    "name": clean_name,
                    "url": u,
                    "group": group,
                    "_source": name
                }
    
    total_lives = len(all_lives)
    print(f"    共提取到 {total_lives} 条直播源 (去重后)")

    # ── 4. 并发测速直播流 ──
    print(f">>> 并发测试 {total_lives} 条直播流，剔除死链...")
    valid_lives = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        futures = [pool.submit(test_live_stream, l) for l in all_lives.values()]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            res = future.result()
            if res:
                valid_lives.append(res)
            if i % 50 == 0 or i == total_lives:
                sys.stdout.write(f"\r    进度: {i}/{total_lives} (可用: {len(valid_lives)})")
                sys.stdout.flush()
    print()

    if not valid_lives:
        print("❌ 没有存活的直播源，任务终止。")
        return 1

    # ── 5. 按分组整理并生成 TXT ──
    print(">>> 按分组生成 zhibo.txt...")
    grouped = {}
    for live in valid_lives:
        # 清洗分组名，确保没有多余的逗号和换行
        g = re.sub(r'[,\n\r]', '', live["group"]).strip() or "未分组"
        if g not in grouped:
            grouped[g] = []
        
        # 清洗频道名
        name = re.sub(r'[,\n\r]', '', live["name"]).strip()
        # 格式: 频道名,url (可加入延迟备注: 频道名 [12ms],url)
        line = f"{name},{live['url']}"
        if line not in grouped[g]:
            grouped[g].append(line)

    # 写入文件，使用标准的 TXT 分组格式 (#genre#)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for group, lines in grouped.items():
            f.write(f"{group},#genre#\n")
            for line in lines:
                f.write(f"{line}\n")
            f.write("\n")

    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 采集完成！")
    print(f"✅ 结果已保存至: {OUTPUT_FILE}")
    print(f"✅ 最终存活: {len(valid_lives)} 个频道，共 {len(grouped)} 个分组")
    
    # 打印排行前 5 的分组统计
    top_groups = sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    for g, l in top_groups:
        print(f"   - {g}: {len(l)} 个频道")

    return 0

if __name__ == "__main__":
    sys.exit(main())
