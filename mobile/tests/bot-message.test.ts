import assert from "node:assert/strict";
import test from "node:test";
import { botPhoneNumber } from "../src/lib/bot-message.ts";

test("accepts an international bot number and strips display separators", () => {
  assert.equal(botPhoneNumber(" +1 (202) 555-0142 "), "+12025550142");
});

test("rejects local numbers, multiple recipients, SMS links and other arbitrary input", () => {
  for (const value of ["2025550142", "+0000000", "+123456", "+1234567890123456",
    "+12025550142,+12025550143", "sms:+12025550142", "+12025550142?body=hey", "+1\n2025550142"]) {
    assert.equal(botPhoneNumber(value), null);
  }
});
