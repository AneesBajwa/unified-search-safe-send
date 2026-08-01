/** The envelope every refusal arrives in (api-design.md, Errors). */
export interface ApiErrorBody {
  error?: {
    code: string;
    classification: "transient" | "permanent" | "needs_reconnect" | "config";
    message: string;
    reconnect_url?: string;
  };
}
