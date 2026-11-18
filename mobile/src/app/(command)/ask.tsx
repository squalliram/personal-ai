import { useState } from "react";
import { ActivityIndicator, KeyboardAvoidingView, Platform, Text, TextInput } from "react-native";
import { Button, Card, ErrorNote, Page, color, type } from "../../components/ui";
import { ApiError, problem } from "../../lib/api";
import { api, useAuth } from "../../lib/auth";
import { demoQuestions } from "../../lib/demo";
import { useResource } from "../../lib/use-resource";

export default function Ask() {
  const { access, invalidate } = useAuth();
  const { data: available, error: statusError, loading, reload } = useResource(api.assistantAvailable, true);
  const [question, setQuestion] = useState("");
  const [reply, setReply] = useState<{ question: string; answer: string }>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(text: string) {
    const clean = text.trim();
    if (!clean || busy || !available || access.kind !== "live") return;
    setBusy(true);
    setError("");
    try {
      const { answer } = await api.ask(access.session, clean);
      setReply({ question: clean, answer });
      setQuestion("");
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) invalidate(access.session.token);
      else setError(problem(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Page title="Ask Jarvis">
        <Text style={type.body}>{access.kind === "demo"
          ? "Try a sample question. Answers are examples, and nothing is sent to Jarvis."
          : "Ask about the information in your Command Centre. Jarvis won’t take actions from this screen."}</Text>
        {statusError && <ErrorNote message={statusError} retry={reload} />}
        {loading && <ActivityIndicator color={color.accent} accessibilityLabel="Checking assistant availability" />}
        {!loading && available === false && <Card><Text style={type.muted}>The assistant is not configured.</Text></Card>}
        {access.kind === "demo" && demoQuestions.map((sample, index) => (
          <Button key={sample.question} primary={index === 0} label={sample.question} onPress={() => setReply(sample)} />
        ))}
        {access.kind === "live" && available && <>
          <TextInput accessibilityLabel="Your question for Jarvis" placeholder="What needs my attention today?"
            placeholderTextColor={color.muted} style={[type.input, { minHeight: 108, textAlignVertical: "top" }]}
            editable={!busy} multiline maxLength={500} value={question} onChangeText={setQuestion} />
          <Button primary label={busy ? "Asking…" : "Ask Jarvis"} disabled={!question.trim() || busy}
            onPress={() => void submit(question)} />
        </>}
        {error && <ErrorNote message={error} />}
        {reply && <Card>
          <Text style={type.eyebrow}>{access.kind === "demo" ? "SAMPLE RESPONSE" : "JARVIS"}</Text>
          <Text style={type.section}>{reply.question}</Text>
          <Text accessibilityLiveRegion="polite" selectable style={type.body}>{reply.answer}</Text>
        </Card>}
      </Page>
    </KeyboardAvoidingView>
  );
}
