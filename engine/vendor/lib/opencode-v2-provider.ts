// A distinct package identity keeps V2 from replacing this transport with its
// native OpenAI driver before the multi-account fetch hook can be installed.
export { createOpenAI } from "@ai-sdk/openai";
