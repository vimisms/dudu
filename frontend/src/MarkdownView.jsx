import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";

/**
 * Renders task output as rich text: code/scripts become highlighted code
 * blocks, prose becomes paragraphs, lists/tables via GFM.
 */
export default function MarkdownView({ text }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {text || ""}
      </ReactMarkdown>
    </div>
  );
}
