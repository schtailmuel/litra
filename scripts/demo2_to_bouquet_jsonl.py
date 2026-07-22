#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


def language_tag(language, script):
    language = str(language or "").strip()
    script = str(script or "").strip()
    if not language:
        return ""
    if not script:
        return language
    if language.endswith(f"_{script}"):
        return language
    return f"{language}_{script}"


def bouquet_rows(payload, split):
    translations = payload.get("translations")
    if not isinstance(translations, list):
        raise ValueError("Expected top-level key 'translations' to contain a list.")

    for row_number, item in enumerate(translations, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Translation row {row_number}: expected an object.")

        pid = str(item.get("pid", "")).strip()
        if not pid:
            raise ValueError(f"Translation row {row_number}: missing pid.")

        sentences = item.get("sentences")
        if not isinstance(sentences, list):
            raise ValueError(f"{pid}: expected 'sentences' to contain a list.")

        lang_tag = language_tag(item.get("language", ""), item.get("script", ""))
        for sentence_index, sentence in enumerate(sentences, start=1):
            text = "" if sentence is None else str(sentence)
            yield {
                "uniq_id": f"{pid}-S{sentence_index}",
                "src_text": text,
                "domain": "<na>",
                "par_comment": "<na>",
                "orig_text": text,
                "tgt_text": text,
                "newline_next": False,
                "tags": "<na>",
                "register": "<na>",
                "par_id": pid,
                "split": split,
                "level": "sentence_level",
                "src_lang": lang_tag,
                "tgt_lang": lang_tag,
            }


def convert(input_path, output_path, split):
    with input_path.open("r", encoding="utf-8") as source_file:
        payload = json.load(source_file)

    count = 0
    output_stream = sys.stdout if output_path is None else output_path.open("w", encoding="utf-8")
    try:
        for row in bouquet_rows(payload, split):
            output_stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            output_stream.write("\n")
            count += 1
    finally:
        if output_path is not None:
            output_stream.close()
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Convert demo-2/Bouquet nested JSON into sentence-level Bouquet JSONL."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="demo-2.json",
        type=Path,
        help="Input JSON file with top-level translations list. Default: demo-2.json",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        type=Path,
        help="Output JSONL path. Omit to write JSONL to stdout.",
    )
    parser.add_argument(
        "--split",
        default="dev",
        help="Value for the Bouquet split field. Default: dev",
    )
    args = parser.parse_args()

    try:
        count = convert(args.input, args.output, args.split)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.output is not None:
        print(f"Wrote {count} sentence rows to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
