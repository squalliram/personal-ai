import { useRef } from "react";
import { Platform, Text, TextInput, View } from "react-native";
import { Button, color, type } from "./ui";

export function DraftInput({ label, dictateLabel, placeholder, value, onChange, maxLength, editable }: {
  label: string;
  dictateLabel: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  maxLength: number;
  editable: boolean;
}) {
  const input = useRef<TextInput>(null);
  return (
    <View style={{ gap: 8 }}>
      <TextInput ref={input} accessibilityLabel={label} placeholder={placeholder} placeholderTextColor={color.muted}
        style={[type.input, { minHeight: 108, textAlignVertical: "top" }]} editable={editable}
        multiline maxLength={maxLength} value={value} onChangeText={onChange} />
      <Button label={dictateLabel} disabled={!editable} onPress={() => input.current?.focus()} />
      <Text style={type.muted}>{Platform.OS === "ios"
        ? "Use the microphone on your iPhone keyboard, then review the text."
        : "Use your keyboard’s microphone if it has one, then review the text."}</Text>
    </View>
  );
}
