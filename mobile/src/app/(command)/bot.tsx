import * as SecureStore from "expo-secure-store";
import * as SMS from "expo-sms";
import { useEffect, useState } from "react";
import { Keyboard, KeyboardAvoidingView, Linking, Platform, Text, TextInput } from "react-native";
import { DraftInput } from "../../components/draft-input";
import { Button, Card, ErrorNote, Page, color, type } from "../../components/ui";
import { useAuth } from "../../lib/auth";
import { botPhoneNumber } from "../../lib/bot-message";

const RECIPIENT_KEY = "jarvis-devin-bot-recipient-v1";
const SETUP = "https://docs.devin.ai/work-with-devin/bots/imessage#link-your-number";

export default function DevinBot() {
  const { access } = useAuth();
  const live = Platform.OS === "ios" && access.kind === "live";
  const [recipient, setRecipient] = useState("");
  const [numberInput, setNumberInput] = useState("");
  const [editing, setEditing] = useState(false);
  const [ready, setReady] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!live) return;
    let active = true;
    void SecureStore.getItemAsync(RECIPIENT_KEY).then((value) => {
      if (active && value) setRecipient(botPhoneNumber(value) ?? "");
    }).catch(() => {
      if (active) setError("Couldn’t load the saved bot number.");
    }).finally(() => {
      if (active) setReady(true);
    });
    return () => { active = false; };
  }, [live]);

  async function saveNumber() {
    if (!live || !ready || busy) return;
    const number = botPhoneNumber(numberInput);
    if (!number) {
      setError("Enter the bot’s phone number including its + country code.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await SecureStore.setItemAsync(RECIPIENT_KEY, number, {
        keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
      });
      setRecipient(number);
      setNumberInput("");
      setEditing(false);
      Keyboard.dismiss();
    } catch {
      setError("Couldn’t save the bot number on this device.");
    } finally { setBusy(false); }
  }

  async function removeNumber() {
    if (!live || busy) return;
    setBusy(true);
    setError("");
    try {
      await SecureStore.deleteItemAsync(RECIPIENT_KEY);
      setRecipient("");
      setNumberInput("");
      setEditing(false);
    } catch {
      setError("Couldn’t remove the saved bot number.");
    } finally { setBusy(false); }
  }

  async function open(url: string) {
    setError("");
    try { await Linking.openURL(url); } catch { setError("Couldn’t open that link on this device."); }
  }

  async function compose() {
    const message = draft.trim();
    if (!live || !recipient || editing || !message || busy) return;
    setBusy(true);
    setError("");
    Keyboard.dismiss();
    try {
      if (!(await SMS.isAvailableAsync())) {
        setError("Messages isn’t available on this device.");
        return;
      }
      const { result } = await SMS.sendSMSAsync([recipient], message);
      if (result === "sent") setDraft("");
    } catch {
      setError("Couldn’t open the message composer. Your draft is still here.");
    } finally { setBusy(false); }
  }

  if (Platform.OS !== "ios") return (
    <Page title="Devin Bot"><Text style={type.body}>This Messages shortcut is available on iPhone.</Text></Page>
  );

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior="padding">
      <Page title="Devin Bot">
        <Text style={type.body}>Text your linked Devin Bot through iMessage. The bot’s replies stay in Messages.</Text>
        {live && ready && (!recipient || editing) && <Card>
          <Text style={type.section}>{recipient ? "Change bot number" : "Link your bot in Devin first"}</Text>
          <Text style={type.muted}>Once linked, enter the bot’s number from the Messages conversation, not your own. Don’t enter the one-time linking code here.</Text>
          {!recipient && <Button label="Devin’s iMessage setup guide" onPress={() => void open(SETUP)} />}
          <TextInput accessibilityLabel="Devin Bot phone number" style={type.input} placeholder="+ country code and number"
            placeholderTextColor={color.muted} inputMode="tel" autoCorrect={false} editable={!busy}
            maxLength={32} value={numberInput} onChangeText={setNumberInput} />
          <Button label="Save bot number" onPress={() => void saveNumber()} disabled={!numberInput.trim() || busy} />
          {recipient && <Button label="Cancel change" disabled={busy} onPress={() => { setEditing(false); setError(""); }} />}
        </Card>}
        {live && ready && !!recipient && !editing && <Card>
          <Text style={type.muted}>Saved bot number ending in {recipient.slice(-4)}</Text>
          <Button label="Change number" disabled={busy} onPress={() => { setNumberInput(recipient); setEditing(true); setError(""); }} />
          <Button label="Remove saved number" disabled={busy} onPress={() => void removeNumber()} />
        </Card>}
        {!live && <Text style={type.muted}>Try drafting a text below. Demo mode won’t open Messages or send anything.</Text>}
        {(!live || (!!recipient && !editing)) && <>
          <DraftInput label="Message for Devin Bot" dictateLabel="Dictate a message" placeholder="What needs my attention today?"
            maxLength={2000} value={draft} onChange={setDraft} editable={!busy} />
          <Button primary label="Review in Messages" disabled={!live || !draft.trim() || busy}
            onPress={() => void compose()} />
          {live && <Text style={type.muted}>Check the recipient and send it yourself. Jarvis never sends automatically.</Text>}
        </>}
        {error && <ErrorNote message={error} />}
        {live && <Button label="Open Messages" onPress={() => void open("sms:")} disabled={busy} />}
      </Page>
    </KeyboardAvoidingView>
  );
}
