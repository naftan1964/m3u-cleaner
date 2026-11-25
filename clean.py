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
      return b""
    return resp.read(max_bytes)


def is_valid_ts_data(data):
  if not data or len(data) < 1880:
    return False
  head = data.split(b"\n", 1)[0].lstrip().lower()
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


def check_url(url):
  try:
    data = fetch_bytes(url, max_bytes=8192)
    if not data:
      return False

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
          return False
        next_url = urljoin(current_url, sub_links[0])
        if ".m3u8" in next_url.lower() and next_url != current_url:
          sub_data = fetch_bytes(next_url, max_bytes=8192)
          if not sub_data or not sub_data.lstrip().startswith(b"#EXTM3U"):
            return False
          current_url = next_url
          text = sub_data.decode("utf-8", errors="ignore")
        else:
          seg_data = fetch_bytes(next_url, max_bytes=16384)
          return is_valid_ts_data(seg_data)
      return False

    return is_valid_ts_data(data)
  except Exception:
    return False


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


def load_keys_data(filepath=KEYS_FILE):
  if not os.path.exists(filepath):
    return [], {}, {}

  raw_lines = []
  with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      item = line.strip()
      if item and not item.startswith("#"):
        raw_lines.append(item)

  unique_items = list(dict.fromkeys(raw_lines))
  tokens = []
  by_tail = {}
  by_name = {}

  for item in unique_items:
    if item.startswith(("http://", "https://")):
      parsed = urlparse(item)
      parts = [p for p in parsed.path.split("/") if p]
      if parts:
        last_part = parts[-1]
        if "." in last_part and len(parts) >= 2:
          tokens.append(parts[-2])
        else:
          tokens.append(last_part)

      try:
        pl_lines = load_playlist_lines(item)
        pl_entries = parse_playlist(pl_lines)
        for _, h_lines, stream_url in pl_entries:
          if "err.cdntv" in stream_url:
            continue
          sp = urlparse(stream_url)
          s_parts = [p for p in sp.path.split("/") if p]
          if len(s_parts) >= 2:
            tail = "/".join(s_parts[-2:])
            by_tail.setdefault(tail, []).append(stream_url)
          for h in h_lines:
            if h.startswith("#EXTINF") and "," in h:
              _, ch_name = h.rsplit(",", 1)
              ch_key = ch_name.strip().lower()
              if ch_key:
                by_name.setdefault(ch_key, []).append(stream_url)
      except Exception:
        pass
    else:
      tokens.append(item)

  unique_tokens = list(dict.fromkeys(tokens))
  print(
      f"Извлечено уникальных ключей из {filepath}: {len(unique_tokens)}"
      f" ({', '.join(unique_tokens)})"
  )
  return unique_tokens, by_tail, by_name


def parse_playlist(lines):
  entries = []
  header_lines = []
  for line in lines:
    stripped = line.strip()
    if not stripped or stripped == "#EXTM3U":
      continue
    if stripped.startswith("#"):
      header_lines.append(stripped)
    else:
      entries.append((len(entries), header_lines, stripped))
      header_lines = []
  return entries


def extract_channel_name(header_lines):
  for h in header_lines:
    if h.startswith("#EXTINF") and "," in h:
      _, name = h.rsplit(",", 1)
      return name.strip().lower()
  return ""


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

  ch_name = extract_channel_name(header_lines)
  if ch_name:
    for u in by_name.get(ch_name, []):
      candidates.append(u)

  return list(dict.fromkeys(candidates))


def process_entry(entry, tokens, by_tail, by_name, working_prefix_cache):
  idx, header_lines, url = entry

  if check_url(url):
    return (idx, "alive", header_lines, url)

  if tokens or by_tail or by_name:
    parsed = urlparse(url)
    old_parts = [p for p in parsed.path.split("/") if p]
    host = parsed.netloc

    cached_prefix = working_prefix_cache.get(host)
    if cached_prefix and len(old_parts) >= 2:
      fast_url = cached_prefix.rstrip("/") + "/" + "/".join(old_parts[-2:])
      if fast_url != url and check_url(fast_url):
        return (idx, "fixed", header_lines, fast_url)

    for candidate in build_candidate_urls(
        url, header_lines, tokens, by_tail, by_name
    ):
      if candidate != url and check_url(candidate):
        cp = urlparse(candidate)
        c_parts = [p for p in cp.path.split("/") if p]
        if len(c_parts) >= 3:
          prefix = f"{cp.scheme}://{cp.netloc}/" + "/".join(c_parts[:-2])
          working_prefix_cache[host] = prefix
        return (idx, "fixed", header_lines, candidate)

  return (idx, "dead", header_lines, url)


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
  tokens, by_tail, by_name = load_keys_data(KEYS_FILE)

  print(f"Найдено каналов: {len(entries)}")
  if not (tokens or by_tail or by_name):
    print(f"Файл {KEYS_FILE} пуст или не найден — работаю в режиме очистки.")

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
      idx, status, header_lines, final_url = future.result()
      results[idx] = (status, header_lines, final_url)
      done_count += 1
      if done_count % 25 == 0 or done_count == len(entries):
        print(f"Проверено: {done_count}/{len(entries)}")

  alive_count = 0
  fixed_count = 0
  dead_count = 0

  with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
    out.write("#EXTM3U\n")
    for status, header_lines, final_url in results:
      if status in ("alive", "fixed"):
        if status == "alive":
          alive_count += 1
        else:
          fixed_count += 1
        for h in header_lines:
          out.write(h + "\n")
        out.write(final_url + "\n")
      else:
        dead_count += 1

  elapsed = round(time.time() - start_time, 1)
  print("\nГотово!")
  print(f"Рабочих изначально: {alive_count}")
  print(f"Восстановлено через {KEYS_FILE}: {fixed_count}")
  print(f"Удалено нерабочих: {dead_count}")
  print(f"Сохранено в {OUTPUT_FILE} за {elapsed} сек.")


if __name__ == "__main__":
  main()


