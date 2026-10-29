import React from 'react';

// The assistant writes plain text with light markdown: paragraphs, "- "
// bullets, "1. " numbered lists and **bold**. It is shown as React nodes,
// never as HTML, so nothing in a reply can inject markup.

type Block =
  | { kind: 'paragraph'; lines: string[] }
  | { kind: 'bullets'; items: string[] }
  | { kind: 'numbers'; items: string[] };

const BULLET = /^\s*[-*•]\s+/;
const NUMBER = /^\s*\d+[.)]\s+/;

function toBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  for (const rawLine of text.replace(/\r\n/g, '\n').split('\n')) {
    const line = rawLine.trimEnd();
    const last = blocks[blocks.length - 1];
    if (line.trim() === '') {
      blocks.push({ kind: 'paragraph', lines: [] });
      continue;
    }
    if (BULLET.test(line)) {
      const item = line.replace(BULLET, '');
      if (last?.kind === 'bullets') last.items.push(item);
      else blocks.push({ kind: 'bullets', items: [item] });
      continue;
    }
    if (NUMBER.test(line)) {
      const item = line.replace(NUMBER, '');
      if (last?.kind === 'numbers') last.items.push(item);
      else blocks.push({ kind: 'numbers', items: [item] });
      continue;
    }
    if (last?.kind === 'paragraph') last.lines.push(line);
    else blocks.push({ kind: 'paragraph', lines: [line] });
  }
  return blocks.filter((block) => block.kind !== 'paragraph' || block.lines.length > 0);
}

function inline(text: string): React.ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) => (
    part.startsWith('**') && part.endsWith('**') && part.length > 4
      ? <strong key={index} className="font-semibold">{part.slice(2, -2)}</strong>
      : <React.Fragment key={index}>{part}</React.Fragment>
  ));
}

export function AssistantText({ text }: { text: string }) {
  return (
    <div data-component="assistantText" className="flex flex-col gap-3 text-[15px] leading-6 sm:text-base">
      {toBlocks(text).map((block, index) => {
        if (block.kind === 'bullets') {
          return (
            <ul key={index} className="flex list-disc flex-col gap-1 ps-5">
              {block.items.map((item, i) => <li key={i}>{inline(item)}</li>)}
            </ul>
          );
        }
        if (block.kind === 'numbers') {
          return (
            <ol key={index} className="flex list-decimal flex-col gap-1 ps-5">
              {block.items.map((item, i) => <li key={i}>{inline(item)}</li>)}
            </ol>
          );
        }
        return (
          <p key={index} className="m-0 text-pretty">
            {block.lines.map((line, i) => (
              <React.Fragment key={i}>
                {i > 0 && <br />}
                {inline(line)}
              </React.Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}
