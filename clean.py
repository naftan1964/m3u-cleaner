import concurrent.futures
import os
import re
import sys
import time
from urllib.parse import urljoin
import urllib.request

USER_AGENT = "Mozilla/5.0"
TIMEOUT = 10
OUTPUT_FILE = "cleaned_playlist.m3u"


def fetch_bytes(url, max_bytes=16384, timeout=TIMEOUT):
  req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    if resp.status != 200:
      return b""
    return resp.read(max_bytes)


def is_valid_ts_data(data):
  if not data or len(data) < 1880:
    return False
  head = data.split(b"\n", 1).pop(0).lstrip().lower()
  if head.startswith(
      (b"<!doctype", b"<html", b"404", b"403", b"error", b"no signal")
  ):
    return False
  sync_pos = data.find(0x47)
  if sync_pos == -1:
    return False
  mv = memoryview(data)
  packets = 0
  for pos in range(sync_pos, len(data) - 4, 188):
    if mv[pos] != 0x47:
      return False
    pid = ((mv[pos + 1] & 0x1F) << 8) | mv[pos + 2]
    scrambling = (mv[pos + 3] & 0xC0) >> 6
    has_payload = (mv[pos + 3] & 0x10) != 0
    if 0x0020 <= pid < 0x1FFF and scrambling == 0 and has_payload:
      packets += 1
      if packets >= 3:
        return True
  return False


def check_entry(entry):
  idx, name, group, header_lines, url = entry
  try:
    data = fetch_bytes(url, max_bytes=8192)
    if not data:
      return (idx, False)

    if data.lstrip().startswith(b"#EXTM3U"):
      current_url = url
      text = data.decode("utf-8", errors="ignore")
      for _ in range(2):
        sub_links = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not sub_links:
          return (idx, False)
        next_url = urljoin(current_url, sub_links[0])
        if ".m3u8" in next_url.lower() and next_url != current_url:
          sub_data = fetch_bytes(next_url, max_bytes=8192)
          if not sub_data or not sub_data.lstrip().startswith(b"#EXTM3U"):
            return (idx, False)
          current_url = next_url
          text = sub_data.decode("utf-8", errors="ignore")
        else:
          seg_data = fetch_bytes(next_url, max_bytes=16384)
          return (idx, is_valid_ts_data(seg_data))
      return (idx, False)

    return (idx, is_valid_ts_data(data))
  except Exception:
    return (idx, False)


def load_playlist_lines(source):
  source = source.strip()
  if source.startswith(("http://", "https://")):
    if "dropbox.com" in source and "dl=0" in source:
      source = source.replace("dl=0", "dl=1")
    print("🌐 Загружаю плейлист по ссылке...")
    req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
      return resp.read().decode("utf-8", errors="ignore").splitlines()
  else:
    if not os.path.exists(source):
      print(f"❌ Ошибка: файл '{source}' не найден в репозитории!")
      sys.exit(1)
    print(f"📂 Открываю локальный файл: {source}")
    with open(source, "r", encoding="utf-8", errors="ignore") as f:
      return f.read().splitlines()


def main():
  source = os.environ.get("TARGET_SOURCE", "").strip()
  if not source:
    print("❌ Не указана ни ссылка, ни имя файла для проверки!")
    sys.exit(1)

  lines = load_playlist_lines(source)
  top_header = "#EXTM3U"
  entries = []
  header_lines = []
  current_name = "Без названия"
  current_group = "Без группы"

  for raw in lines:
    line = raw.strip()
    if not line:
      continue
    if line.startswith("#EXTM3U"):
      top_header = line
    elif line.startswith("#EXTINF"):
      header_lines = [line]
      current_name = line.rsplit(",", 1)[-1].strip()
      m_grp = re.search(r'group-title="([^"]*)"', line)
      current_group = m_grp.group(1) if m_grp else "Без группы"
    elif line.startswith("#") and header_lines:
      header_lines.append(line)
      if line.startswith("#EXTGRP:"):
        current_group = line.split(":", 1)[1].strip()
    elif line.startswith(("http://", "https://")) and header_lines:
      entries.append(
          (len(entries), current_name, current_group, header_lines, line)
      )
      header_lines = []
      current_group = "Без группы"

  if not entries:
    print("⚠️ В плейлисте не найдено ни одного канала!")
    sys.exit(0)

  print(f"🔍 Начинаю проверку {len(entries)} каналов...")
  results = {}
  with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
    for idx, is_alive in executor.map(check_entry, entries):
      results[idx] = is_alive

  alive_entries = []
  dead_entries = []
  for entry in entries:
    if results.get(entry[0]):
      alive_entries.append(entry)
    else:
      dead_entries.append(entry)

  with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
    out.write(top_header + "\n")
    for _, _, _, h_lines, url in alive_entries:
      for h in h_lines:
        out.write(h + "\n")
      out.write(url + "\n")

  print("\n" + "=" * 45)
  print("🧹 ОТЧЁТ О ЧИСТКЕ ПЛЕЙЛИСТА")
  print("=" * 45)
  print(f"📋 Всего было каналов: {len(entries)}")
  print(f"✅ Оставлено рабочих: {len(alive_entries)}")
  print(f"🗑️ Удалено мёртвых: {len(dead_entries)}")
  if dead_entries:
    print("\n❌ Список удалённых каналов:")
    for num, (_, name, grp, _, _) in enumerate(dead_entries, 1):
      print(f"  {num}. {name} (группа: {grp})")
  else:
    print("\n🎉 Все каналы в листе полностью рабочие!")
  print("=" * 45)
  print(f"💾 Готовый файл сохранён как: {OUTPUT_FILE}\n")


if __name__ == "__main__":
  main()
