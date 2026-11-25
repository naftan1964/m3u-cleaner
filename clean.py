import concurrent.futures
import os
import sys
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
import urllib.request

USER_AGENT = "Mozilla/5.0"
TIMEOUT = 10
OUTPUT_FILE = os.environ.get("OUT_NAME") or "cleaned_playlist.m3u"
KEYS_FILE = "keys.txt"
DELETED_FILE = "deleted_channels.txt"
MAX_WORKERS = 4

STATIC_SEGMENTS = {
    "iptv",
    "live",
    "hls",
    "stream",
    "streams",
    "tv",
    "udp",
    "http",
    "play",
    "playlist",
    "index",
    "mono",
    "video",
    "tracks-v1a1",
    "ch",
    "channel",
}


def fetch_bytes(url, max_bytes=16384, timeout=TIMEOUT):
  req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    if resp.status != 200:
      return b"", f"HTTP {resp.status}"
    return resp.read(max_bytes), "OK"


def is_valid_ts_data(data):
  if not data or len(data) < 1880:
    return False, "пустой или слишком короткий ответ"
  head = data.split(b"\n", 1)[0].lstrip().lower()
  if head.startswith(
      (b"<!doctype", b"<html", b"404", b"403", b"error", b"no signal")
  ):
    return False, "HTML-страница или заглушка вместо видео"
  sync_pos = data.find(0x47)
  if sync_pos == -1:
    return False, "нет видеопакетов MPEG-TS (возможно fMP4 или аудио)"
  mv = memoryview(data)
  packets = 0
  for pos in range(sync_pos, len(data) - 4, 188):
    if mv[pos] != 0x47:
      return False, "повреждённый поток MPEG-TS"
    pid = ((mv[pos + 1] & 0x1F) << 8) | mv[pos + 2]
    scrambling = (mv[pos + 3] & 0xC0) >> 6
    has_payload = (mv[pos + 3] & 0x10) != 0
    if 0x0020 <= pid < 0x1FFF and scrambling == 0 and has_payload:
      packets += 1
      if packets >= 3:
        return True, "OK"
  return False, "нет незашифрованного видеопотока"


def check_url_detailed(url):
  if "err.cdntv" in url:
    return False, "заглушка провайдера (err.cdntv)"
  try:
    data, status = fetch_bytes(url, max_bytes=8192)
    if not data:
      return False, status

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
          return False, "пустой m3u8-плейлист"
        next_url = urljoin(current_url, sub_links[0])
        if "err.cdntv" in next_url:
          return False, "заглушка провайдера (err.cdntv)"
        if ".m3u8" in next_url.lower() and next_url != current_url:
          sub_data, sub_status = fetch_bytes(next_url, max_bytes=8192)
          if not sub_data:
            return False, sub_status
          if not sub_data.lstrip().startswith(b"#EXTM3U"):
            return False, "ошибка вложенного m3u8"
          current_url = next_url
          text = sub_data.decode("utf-8", errors="ignore")
        else:
          seg_data, seg_status = fetch_bytes(next_url, max_bytes=16384)
          if not seg_data:
            return False, seg_status
          return is_valid_ts_data(seg_data)
      return False, "слишком глубокая вложенность m3u8"

    return is_valid_ts_data(data)
  except urllib.error.HTTPError as e:
    return False, f"HTTP ошибка {e.code}"
  except urllib.error.URLError as e:
    return False, f"ошибка сети / таймаут ({e.reason})"
  except TimeoutError:
    return False, "таймаут ожидания ответа"
  except Exception as e:
    return False, f"ошибка соединения ({type(e).__name__}: {e})"



def check_url(url):
  ok, _ = check_url_detailed(url)
  return ok


def load_playlist_lines(source):
  source = source.strip()
  if source.startswith(("http://", "https://")):
    if "dropbox.com" in source and "dl=0" in source:
      source = source.replace("dl=0", "dl=1")
    print("Загружаю плейлист по ссылке...")
    req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
      raw = resp.read().decode("utf-8", errors="ignore")
      return raw.splitlines()
  if not os.path.exists(source):
    raise FileNotFoundError(f"Файл не найден: {source}")
  print(f"Читаю локальный файл: {source}")
  with open(source, "r", encoding="utf-8", errors="ignore") as f:
    return f.read().splitlines()


def parse_playlist(lines):
  entries = []
  header_lines = []
  for line_num, line in enumerate(lines, 1):
    stripped = line.strip()
    if not stripped or stripped == "#EXTM3U":
      continue
    if stripped.startswith("#"):
      header_lines.append(stripped)
    else:
      entries.append((len(entries), header_lines, stripped, line_num))
      header_lines = []
  return entries


def extract_channel_name(header_lines, raw=False):
  for h in header_lines:
    if h.startswith("#EXTINF") and "," in h:
      _, name = h.rsplit(",", 1)
      return name.strip() if raw else name.strip().lower()
  return "Без названия" if raw else ""


def extract_channel_group(header_lines):
  for h in header_lines:
    if h.startswith("#EXTGRP:"):
      return h.split(":", 1)[1].strip()
  for h in header_lines:
    if h.startswith("#EXTINF") and 'group-title="' in h:
      part = h.split('group-title="', 1)[1]
      return part.split('"', 1)[0].strip()
  return "Без группы"



def extract_token_from_item(item):
  clean = item.strip()
  if clean.startswith(("http://", "https://")):
    parsed = urlparse(clean)
    parts = [p for p in parsed.path.split("/") if p]
    for part in reversed(parts):
      if "." in part or part.isdigit() or len(part) < 6:
        continue
      if part.lower() in STATIC_SEGMENTS:
        continue
      return part
    return ""
  return clean


def build_candidate_urls(old_url, header_lines, tokens, by_tail, by_name):
  candidates = []
  parsed = urlparse(old_url)
  old_parts = [p for p in parsed.path.split("/") if p]

  for token in tokens:
    if parsed.query:
      q_pairs = parse_qsl(parsed.query, keep_blank_values=True)
      for idx, (qk, qv) in enumerate(q_pairs):
        if qk.lower() in {"token", "key", "hash", "auth", "pass", "id"} or (
            len(qv) >= 6 and qv.isalnum()
        ):
          new_q = list(q_pairs)
          new_q[idx] = (qk, token)
          candidates.append(
              urlunparse((
                  parsed.scheme,
                  parsed.netloc,
                  parsed.path,
                  parsed.params,
                  urlencode(new_q),
                  "",
              ))
          )

    for i, part in enumerate(old_parts):
      is_last_file = i == len(old_parts) - 1 and (
          "." in part or part.isdigit()
      )
      if is_last_file or part.lower() in STATIC_SEGMENTS:
        continue
      if len(part) >= 5 and part != token:
        new_parts = old_parts[:i] + [token] + old_parts[i + 1 :]
        new_path = "/" + "/".join(new_parts)
        candidates.append(
            urlunparse((
                parsed.scheme,
                parsed.netloc,
                new_path,
                parsed.params,
                parsed.query,
                "",
            ))
        )

  if len(old_parts) >= 2:
    tail = "/".join(old_parts[-2:])
    for u in by_tail.get(tail, []):
      candidates.append(u)

  ch_name = extract_channel_name(header_lines, raw=False)
  if ch_name:
    for u in by_name.get(ch_name, []):
      candidates.append(u)

  return list(dict.fromkeys(candidates))


def load_and_clean_keys(entries, filepath=KEYS_FILE):
  if not os.path.exists(filepath):
    return [], {}, {}

  raw_lines = []
  with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      item = line.strip()
      if item and not item.startswith("#"):
        raw_lines.append(item)

  if not raw_lines:
    return [], {}, {}

  unique_items = list(dict.fromkeys(raw_lines))
  dups_removed = len(raw_lines) - len(unique_items)
  if dups_removed > 0:
    print(f"В {filepath} удалено дубликатов: {dups_removed}")

  domain_samples = {}
  for _, _, u, _ in entries:
    host = urlparse(u).netloc
    if host:
      lst = domain_samples.setdefault(host, [])
      if len(lst) < 3:
        lst.append(u)
  sample_urls = [u for urls in domain_samples.values() for u in urls]

  valid_items = []
  tokens = []
  by_tail = {}
  by_name = {}
  dead_keys_count = 0

  print(f"Предпроверка ключей ({len(unique_items)} шт.)...")

  for item in unique_items:
    token = extract_token_from_item(item)
    is_alive = False
    temp_tail = {}
    temp_name = {}

    # Если это полная ссылка или чистый ключ iLook (14/24 символа) — проверяем через лист
    pl_url = ""
    if item.startswith(("http://", "https://")):
      pl_url = item
    elif token and len(token) in (14, 24) and token.isalnum():
      pl_url = f"https://epg.one{token}"

    if pl_url:
      try:
        pl_lines = load_playlist_lines(pl_url)
        pl_entries = parse_playlist(pl_lines)
        valid_streams = [
            (h, u) for _, h, u, _ in pl_entries if "err.cdntv" not in u
        ]
        for _, test_u in valid_streams[:3]:
          if check_url(test_u):
            is_alive = True

            break
        if is_alive:
          for h_lines, stream_url in valid_streams:
            sp = urlparse(stream_url)
            s_parts = [p for p in sp.path.split("/") if p]
            if len(s_parts) >= 2:
              tail = "/".join(s_parts[-2:])
              temp_tail.setdefault(tail, []).append(stream_url)
            ch_key = extract_channel_name(h_lines, raw=False)
            if ch_key:
              temp_name.setdefault(ch_key, []).append(stream_url)
      except Exception:
        is_alive = False

    # Запасная проверка подстановкой по всем серверам из Main Mix
    if not is_alive and token:
      for su in sample_urls:
        for cand in build_candidate_urls(su, [], [token], {}, {}):
          if check_url(cand):
            is_alive = True
            break
          time.sleep(0.2)
        if is_alive:
          break

    if is_alive:
      valid_items.append(item)
      if token:
        tokens.append(token)
      for k, v in temp_tail.items():
        by_tail.setdefault(k, []).extend(v)
      for k, v in temp_name.items():
        by_name.setdefault(k, []).extend(v)
    else:
      dead_keys_count += 1
      print(f"Удалён нерабочий ключ из {filepath}: {item}")

  if len(valid_items) != len(raw_lines):
    with open(filepath, "w", encoding="utf-8") as f:
      for vi in valid_items:
        f.write(vi + "\n")
    print(
        f"Файл {filepath} обновлён: оставлено рабочих ключей — {len(valid_items)}"
        f" (удалено мёртвых: {dead_keys_count}, дублей: {dups_removed})"
    )

  unique_tokens = list(dict.fromkeys(tokens))
  print(
      f"Активных уникальных ключей для работы: {len(unique_tokens)}"
      f" ({', '.join(unique_tokens)})"
  )
  return unique_tokens, by_tail, by_name


def process_entry(entry, tokens, by_tail, by_name, working_prefix_cache):
  idx, header_lines, url, line_num = entry

  ok, reason = check_url_detailed(url)
  if ok:
    return (idx, "alive", header_lines, url, "", line_num)

  if tokens or by_tail or by_name:
    parsed = urlparse(url)
    old_parts = [p for p in parsed.path.split("/") if p]
    host = parsed.netloc

    cached_prefix = working_prefix_cache.get(host)
    if cached_prefix and len(old_parts) >= 2:
      fast_url = cached_prefix.rstrip("/") + "/" + "/".join(old_parts[-2:])
      if fast_url != url and check_url(fast_url):
        return (idx, "fixed", header_lines, fast_url, "", line_num)

    for candidate in build_candidate_urls(
        url, header_lines, tokens, by_tail, by_name
    ):
      if candidate != url and check_url(candidate):
        cp = urlparse(candidate)
        c_parts = [p for p in cp.path.split("/") if p]
        if len(c_parts) >= 3:
          prefix = f"{cp.scheme}://{cp.netloc}/" + "/".join(c_parts[:-2])
          working_prefix_cache[host] = prefix
        return (idx, "fixed", header_lines, candidate, "", line_num)

  return (idx, "dead", header_lines, url, reason, line_num)

def main():
  args_tail = sys.argv[1:]
  if args_tail:
    source = next(iter(args_tail))
  elif os.environ.get("TARGET_SOURCE"):
    source = os.environ["TARGET_SOURCE"]
  else:
    source = input("Введите ссылку на Main Mix или путь к файлу: ").strip()

  if not source:
    print("Источник не указан.")
    return

  start_time = time.time()
  lines = load_playlist_lines(source)
  entries = parse_playlist(lines)
  print(f"Найдено каналов: {len(entries)}")

  tokens, by_tail, by_name = load_and_clean_keys(entries, KEYS_FILE)
  if not (tokens or by_tail or by_name):
    print(f"Рабочих ключей в {KEYS_FILE} нет — работаю в режиме очистки.")

  working_prefix_cache = {}
  results = [None] * len(entries)
  done_count = 0

  with concurrent.futures.ThreadPoolExecutor(
      max_workers=MAX_WORKERS
  ) as executor:
    futures = [
        executor.submit(
            process_entry, entry, tokens, by_tail, by_name, working_prefix_cache
        )
        for entry in entries
    ]
    for future in concurrent.futures.as_completed(futures):
      idx, status, header_lines, final_url, reason = future.result()
      results[idx] = (status, header_lines, final_url, reason)
      done_count += 1
      if done_count % 25 == 0 or done_count == len(entries):
        print(f"Проверено: {done_count}/{len(entries)}")

  alive_count = 0
  fixed_count = 0
  dead_entries = []

  with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
    out.write("#EXTM3U\n")
    for status, header_lines, final_url, reason in results:
      if status in ("alive", "fixed"):
        if status == "alive":
          alive_count += 1
        else:
          fixed_count += 1
        for h in header_lines:
          out.write(h + "\n")
        out.write(final_url + "\n")
      else:
        ch_title = extract_channel_name(header_lines, raw=True)
        dead_entries.append((ch_title, final_url, reason))

  # Записываем отчёт об удалённых каналах
  now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
  with open(DELETED_FILE, "w", encoding="utf-8") as df:
    df.write(f"Отчёт проверки от {now_str}\n")
    df.write(f"Итоговый файл: {OUTPUT_FILE} | Удалено каналов: {len(dead_entries)}\n")
    df.write("=" * 60 + "\n\n")
    for i, (ch_title, bad_url, reason) in enumerate(dead_entries, 1):
      df.write(f"{i}. {ch_title} — Причина: {reason}\n")
      df.write(f"   {bad_url}\n\n")

  elapsed = round(time.time() - start_time, 1)
  print("\nГотово!")
  print(f"Рабочих изначально: {alive_count}")
  print(f"Восстановлено через {KEYS_FILE}: {fixed_count}")
  print(f"Удалено нерабочих: {len(dead_entries)} (список в {DELETED_FILE})")
  print(f"Сохранено в {OUTPUT_FILE} за {elapsed} сек.")


if __name__ == "__main__":
  main()

