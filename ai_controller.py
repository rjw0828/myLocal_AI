"""Local AI Lab - Phase 1: Python to Ollama chat test (stdlib only)."""
import json
import time
import urllib.error
import urllib.request

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:9b"
SYSTEM_PROMPT = "너는 개인용 로컬 AI 도우미다. 질문에 한국어로 명확하고 간결하게 답한다."


def ask_ollama(messages):
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "think": False
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print(f"Local AI Lab | Model: {MODEL}")
    print("Commands: /exit (quit), /clear (reset chat)\n")
    while True:
        try:
            question = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break
        if not question:
            continue
        if question.lower() == "/exit":
            break
        if question.lower() == "/clear":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("Chat context cleared.\n")
            continue

        user_message = {"role": "user", "content": question}
        started = time.perf_counter()
        try:
            result = ask_ollama(messages + [user_message])
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            print(f"HTTP {error.code}: {details}\n")
            continue
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"Cannot reach Ollama: {error}")
            print("Check that Ollama is running and the model name is correct.\n")
            continue
        except (ValueError, KeyError) as error:
            print(f"Unexpected response: {error}\n")
            continue

        assistant_message = result.get("message", {})
        answer = assistant_message.get("content", "")
        messages.extend([user_message, assistant_message])
        elapsed = time.perf_counter() - started
        tokens = result.get("eval_count", 0)
        duration = result.get("eval_duration", 0)
        speed = tokens / (duration / 1e9) if duration else 0
        print(f"AI > {answer}\n")
        print(f"[Elapsed: {elapsed:.1f}s | Generation: {speed:.1f} tokens/s]\n")


if __name__ == "__main__":
    main()
