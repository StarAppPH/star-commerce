const HEIC_TYPES = ['image/heic', 'image/heif'];
const HEIC_EXTENSIONS = ['.heic', '.heif'];

// Why `file` can't be uploaded, or null when it can: never HEIC, only the
// `allowedTypes` (`typeLabel` says what they are, "a JPG or PNG photo"), under
// `bytesMax`.
export function fileProblem(file: File, allowedTypes: string[], bytesMax: number, typeLabel: string): string | null {
  const name = file.name.toLowerCase();
  if (HEIC_TYPES.includes(file.type) || HEIC_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return `${file.name} is a HEIC photo. Convert it to JPG, PNG or WebP before uploading.`;
  }
  if (!allowedTypes.includes(file.type)) return `${file.name} is not ${typeLabel}.`;
  if (file.size >= bytesMax) return `${file.name} is over ${Math.round(bytesMax / (1024 * 1024))} MB.`;
  return null;
}
