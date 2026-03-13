import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const remarkPlugins = [remarkGfm];

const components: Components = {
  h1: ({ children }) => (
    <h1 className="text-sm font-bold mt-2 mb-1">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-sm font-bold mt-2 mb-1">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-xs font-bold mt-1.5 mb-0.5">{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 className="text-xs font-semibold mt-1 mb-0.5">{children}</h4>
  ),
  p: ({ children }) => <p className="mb-1.5 last:mb-0">{children}</p>,
  code: ({ children }) => (
    <code className="bg-background/50 rounded px-1 py-0.5 text-[11px] font-mono">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="bg-background/50 rounded px-2 py-1.5 overflow-x-auto mb-1.5 last:mb-0 text-[11px] font-mono whitespace-pre [&>code]:bg-transparent [&>code]:p-0 [&>code]:rounded-none">
      {children}
    </pre>
  ),
  ul: ({ children }) => (
    <ul className="list-disc pl-4 mb-1.5 last:mb-0 space-y-0.5">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal pl-4 mb-1.5 last:mb-0 space-y-0.5">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="text-xs">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-400 hover:underline"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-muted-foreground/30 pl-2 italic text-muted-foreground mb-1.5 last:mb-0">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto mb-1.5 last:mb-0">
      <table className="border-collapse text-xs w-full">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border px-2 py-1 text-left font-semibold bg-background/30">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-border px-2 py-1">{children}</td>
  ),
  hr: () => <hr className="border-border my-2" />,
  strong: ({ children }) => (
    <strong className="font-semibold">{children}</strong>
  ),
};

interface ChatMarkdownProps {
  content: string;
}

export default function ChatMarkdown({ content }: ChatMarkdownProps) {
  return (
    <div className="text-xs leading-relaxed">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
