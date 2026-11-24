import concurrent.futures
import os
import sys
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
import urllib.request

USER_AGENT = "Mozilla/5.0"
TIMEOUT = 10
OUTPUT_FILE = "cleaned_playlist.m3u"
KEYS_FILE = "keys.txt"
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


def load_keys(filepath=KEYS_FILE):
  if not os.path.exists(filepath):
    return []
  keys = []
  with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      item = line.strip()
      if item and not item.startswith("#"):
        keys.append(item)
  return keys


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
    

def build_candidate_urls(old_url, key_item):
  candidates = []
  parsed = urlparse(old_url)
  clean_key = key_item.strip()

  if clean_key.startswith(("http://", "https://")):
    kp = urlparse(clean_key.rstrip("/"))
    old_parts = [p for p in parsed.path.split("/") if p]
    key_parts = [p for p in kp.path.split("/") if p]

    if len(old_parts) > len(key_parts):
      tail = "/".join(old_parts[len(key_parts) :])
      new_path = "/" + "/".join(key_parts + [tail])
    else:
      new_path = parsed.path

    candidates.append(
        urlunparse(
            (kp.scheme, kp.netloc, new_path, parsed.params, parsed.query, "")
        )
    )
    return candidates

  if "/" not in clean_key and (":" in clean_key or "." in clean_key):
    candidates.append(
        urlunparse((
            parsed.scheme,
            clean_key,
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        ))
    )
    return candidates

  if "/" in clean_key:
    key_parts = [p for p in clean_key.split("/") if p]
    old_parts = [p for p in parsed.path.split("/") if p]
    k_len = len(key_parts)
    if k_len > 0 and len(old_parts) >= k_len:
      for i in range(len(old_parts) - k_len + 1):
        if (
            old_parts[i] == key_parts[0]
            or old_parts[i + k_len - 1] == key_parts[-1]
        ):
          new_parts = old_parts[:i] + key_parts + old_parts[i + k_len :]
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
      if not candidates and len(old_parts) > k_len:
        new_parts = key_parts + old_parts[k_len:]
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
    return candidates

  if parsed.query:
    q_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for idx, (qk, qv) in enumerate(q_pairs):
      if qk.lower() in {"token", "key", "hash", "auth", "pass", "id"} or (
          len(qv) >= 6 and qv.isalnum()
      ):
        new_q = list(q_pairs)
        new_q[idx] = (qk, clean_key)
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

  old_parts = [p for p in parsed.path.split("/") if p]
  for i, part in enumerate(old_parts):
    is_last_file = i == len(old_parts) - 1 and (
        "." in part or part.isdigit()
    )
    if is_last_file or part.lower() in STATIC_SEGMENTS:
      continue
    if len(part) >= 5:
      new_parts = old_parts[:i] + [clean_key] + old_parts[i + 1 :]
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

  return candidates


def process_entry(entry, keys, working_key_cache):
  idx, header_lines, url = entry

  if check_url(url):
    return (idx, "alive", header_lines, url)

  if keys:
    host = urlparse(url).netloc
    ordered_keys = list(keys)

    cached_key = working_key_cache.get(host)
    if cached_key and cached_key in ordered_keys:
      ordered_keys.remove(cached_key)
      ordered_keys.insert(0, cached_key)

    for key_item in ordered_keys:
      for candidate in build_candidate_urls(url, key_item):
        if candidate != url and check_url(candidate):
          working_key_cache[host] = key_item
          return (idx, "fixed", header_lines, candidate)

  return (idx, "dead", header_lines, url)


def main():
  if len(sys.argv) > 1:
    source = sys.argv[1]
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
  keys = load_keys(KEYS_FILE)

  print(f"Найдено каналов: {len(entries)}")
  if keys:
    print(f"Загружено ключей из {KEYS_FILE}: {len(keys)}")
  else:
    print(f"Файл {KEYS_FILE} пуст или не найден — работаю в режиме очистки.")

  working_key_cache = {}
  results = [None] * len(entries)
  done_count = 0

  with concurrent.futures.ThreadPoolExecutor(
      max_workers=MAX_WORKERS
  ) as executor:
    futures = [
        executor.submit(process_entry, entry, keys, working_key_cache)
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

