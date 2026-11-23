export function botPhoneNumber(input: string): string | null {
  const value = input.trim().replace(/[ ().-]/g, "");
  return /^\+[1-9][0-9]{6,14}$/.test(value) ? value : null;
}
