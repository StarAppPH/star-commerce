'use client';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import Image from 'next/image';
import { ArrowUpIcon, CameraIcon, FileTextIcon, MicIcon, PaperclipIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { InputGroup, InputGroupAddon, InputGroupTextarea } from '@/components/ui/input-group';
import { useSnackbar } from '@/lib/SnackbarContext';
import { cn } from '@/lib/utils';
import { ATTACHMENTS_MAX, attachmentProblem, isPdf, type PickedFile, type UploadedFiles } from './attachments';
import { MESSAGE_MAX_LENGTH } from './useChat';

interface ChatComposerProps {
  // The message: its text, the files uploaded for it (null: none), or both.
  onSend: (text: string, uploaded: UploadedFiles | null) => void;
  // Uploads files to attach; without it the composer attaches none.
  onUpload?: (files: File[]) => Promise<UploadedFiles>;
  onVoice?: () => void;
  disabled?: boolean;
  placeholder: string;
  variant: 'admin' | 'shop';
  autoFocus?: boolean;
  // The big box on a new chat's screen.
  hero?: boolean;
  dataComponent: string;
  className?: string;
}

// The message box. Enter sends; Shift+Enter starts a new line (admin). The
// paperclip attaches photos or a PDF, the camera takes a photo (a phone's
// camera; a computer opens the file picker), the mic opens voice mode.
export function ChatComposer({
  onSend, onUpload, onVoice, disabled = false, placeholder, variant, autoFocus = false, hero = false, dataComponent, className,
}: ChatComposerProps) {
  const { showSnackbar } = useSnackbar();
  const [text, setText] = useState('');
  const [picked, setPicked] = useState<PickedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const pickedRef = useRef<PickedFile[]>([]);
  const locked = disabled || uploading;
  const canSend = (text.trim().length > 0 || picked.length > 0) && !locked;

  // The previews still shown are let go when the composer goes.
  useEffect(() => { pickedRef.current = picked; }, [picked]);
  useEffect(() => () => { pickedRef.current.forEach((one) => { if (one.preview) URL.revokeObjectURL(one.preview); }); }, []);

  const add = (files: FileList | null) => {
    if (!files) return;
    const accepted: PickedFile[] = [];
    let pdfs = picked.filter((one) => isPdf(one.file)).length;
    for (const file of Array.from(files)) {
      const problem = attachmentProblem(file);
      if (problem) {
        showSnackbar(problem, 'error');
        continue;
      }
      if (isPdf(file)) {
        if (pdfs > 0) {
          showSnackbar('Attach at most one PDF per message.', 'error');
          continue;
        }
        pdfs += 1;
      }
      accepted.push({ file, preview: isPdf(file) ? null : URL.createObjectURL(file) });
    }
    const room = ATTACHMENTS_MAX - picked.length;
    if (accepted.length > room) showSnackbar(`Attach up to ${ATTACHMENTS_MAX} files per message.`, 'error');
    accepted.slice(room).forEach((one) => { if (one.preview) URL.revokeObjectURL(one.preview); });
    setPicked([...picked, ...accepted.slice(0, room)]);
  };

  const remove = (one: PickedFile) => {
    if (one.preview) URL.revokeObjectURL(one.preview);
    setPicked((current) => current.filter((other) => other !== one));
  };

  const send = async () => {
    if (!canSend) return;
    let uploaded: UploadedFiles | null = null;
    if (picked.length > 0 && onUpload) {
      setUploading(true);
      try {
        uploaded = await onUpload(picked.map((one) => one.file));
      } catch {
        showSnackbar('The files didn’t upload. Check your connection and try again.', 'error');
        return;
      } finally {
        setUploading(false);
      }
    }
    onSend(text.trim(), uploaded);
    setText('');
    picked.forEach((one) => { if (one.preview) URL.revokeObjectURL(one.preview); });
    setPicked([]);
  };

  const full = picked.length >= ATTACHMENTS_MAX;
  const attachButtons = onUpload && (
    <>
      <FilePickerButton
        dataComponent={`${dataComponent}Attach`}
        inputDataComponent={`${dataComponent}Files`}
        label="Attach photos or a PDF"
        accept="image/jpeg,image/png,application/pdf"
        multiple
        disabled={locked || full}
        variant={variant}
        onPick={add}
      >
        <PaperclipIcon />
      </FilePickerButton>
      <FilePickerButton
        dataComponent={`${dataComponent}Camera`}
        inputDataComponent={`${dataComponent}CameraInput`}
        label="Take a photo"
        accept="image/jpeg,image/png"
        capture="environment"
        disabled={locked || full}
        variant={variant}
        onPick={add}
      >
        <CameraIcon />
      </FilePickerButton>
    </>
  );
  const pickedFiles = picked.length > 0 && (
    <PickedFiles picked={picked} disabled={locked} dataComponent={dataComponent} onRemove={remove} />
  );

  if (variant === 'shop') {
    return (
      <form
        data-component={dataComponent}
        className={cn('flex flex-col gap-2', className)}
        onSubmit={(event) => { event.preventDefault(); send(); }}
      >
        {pickedFiles}
        <div className="flex items-center gap-1.5">
          {attachButtons}
          <label htmlFor={`${dataComponent}Input`} className="sr-only">Message</label>
          <Input
            id={`${dataComponent}Input`}
            data-component={`${dataComponent}Input`}
            className="h-12 min-w-0 flex-1 rounded-full px-1.5 shadow-sm/5 [&_input]:h-11.5 [&_input]:text-base"
            placeholder={placeholder}
            value={text}
            maxLength={MESSAGE_MAX_LENGTH}
            autoFocus={autoFocus}
            autoComplete="off"
            onChange={(event) => setText(event.target.value)}
          />
          {canSend || uploading || !onVoice ? (
            <Button
              data-component={`${dataComponent}Send`}
              type="submit"
              size="icon-xl"
              className="shrink-0 rounded-full"
              disabled={!canSend}
              loading={uploading}
              aria-label="Send"
            >
              <ArrowUpIcon />
            </Button>
          ) : (
            <Button
              data-component={`${dataComponent}Voice`}
              type="button"
              size="icon-xl"
              className="shrink-0 rounded-full"
              onClick={onVoice}
              aria-label="Talk instead"
            >
              <MicIcon />
            </Button>
          )}
        </div>
      </form>
    );
  }

  return (
    <form
      data-component={dataComponent}
      className={className}
      onSubmit={(event) => { event.preventDefault(); send(); }}
    >
      <InputGroup className="rounded-2xl bg-background shadow-sm/5 has-[textarea:focus-visible]:ring-[3px]">
        {pickedFiles && <InputGroupAddon align="block-start">{pickedFiles}</InputGroupAddon>}
        <label htmlFor={`${dataComponent}Input`} className="sr-only">Message</label>
        <InputGroupTextarea
          id={`${dataComponent}Input`}
          data-component={`${dataComponent}Input`}
          className={cn('text-base', hero ? '**:[textarea]:min-h-24' : '**:[textarea]:min-h-12')}
          placeholder={placeholder}
          value={text}
          maxLength={MESSAGE_MAX_LENGTH}
          autoFocus={autoFocus}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              send();
            }
          }}
        />
        <InputGroupAddon align="block-end" className="justify-between gap-1.5">
          <div className="flex items-center gap-1.5">{attachButtons}</div>
          <div className="flex items-center gap-1.5">
            {onVoice && (
              <Button
                data-component={`${dataComponent}Voice`}
                type="button"
                variant="outline"
                size="icon-sm"
                onClick={onVoice}
                aria-label="Talk instead"
              >
                <MicIcon />
              </Button>
            )}
            <Button
              data-component={`${dataComponent}Send`}
              type="submit"
              size="icon-sm"
              disabled={!canSend}
              loading={uploading}
              aria-label="Send"
            >
              <ArrowUpIcon />
            </Button>
          </div>
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}

interface FilePickerButtonProps {
  dataComponent: string;
  inputDataComponent: string;
  label: string;
  accept: string;
  multiple?: boolean;
  // "environment": a phone opens its back camera instead of the files.
  capture?: 'environment';
  disabled: boolean;
  variant: 'admin' | 'shop';
  onPick: (files: FileList | null) => void;
  children: ReactNode;
}

// A button that opens the file picker (or the camera) of a hidden input.
// Only the button is disabled: a disabled input would dim the admin's
// InputGroup, which dims when an input in it is disabled.
function FilePickerButton({
  dataComponent, inputDataComponent, label, accept, multiple = false, capture, disabled, variant, onPick, children,
}: FilePickerButtonProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <Button
        data-component={dataComponent}
        type="button"
        variant={variant === 'shop' ? 'ghost' : 'outline'}
        size={variant === 'shop' ? 'icon-lg' : 'icon-sm'}
        className={cn('shrink-0', variant === 'shop' && 'rounded-full')}
        disabled={disabled}
        aria-label={label}
        onClick={() => inputRef.current?.click()}
      >
        {children}
      </Button>
      <input
        ref={inputRef}
        data-component={inputDataComponent}
        type="file"
        accept={accept}
        multiple={multiple}
        capture={capture}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(event) => { onPick(event.target.files); event.target.value = ''; }}
      />
    </>
  );
}

interface PickedFilesProps {
  picked: PickedFile[];
  disabled: boolean;
  dataComponent: string;
  onRemove: (one: PickedFile) => void;
}

// The files picked for the message: a photo's preview, a PDF's name.
function PickedFiles({ picked, disabled, dataComponent, onRemove }: PickedFilesProps) {
  return (
    <div data-component={`${dataComponent}Picked`} className="flex flex-wrap gap-2">
      {picked.map((one) => (
        <div key={one.preview ?? `${one.file.name}-${one.file.size}`} className="relative">
          {one.preview ? (
            <div className="relative size-16 overflow-hidden rounded-lg bg-muted">
              <Image
                src={one.preview}
                alt={one.file.name}
                fill
                sizes="64px"
                // A preview of a file not uploaded yet (a blob: URL in this
                // browser): no server can fetch it to optimize.
                // eslint-disable-next-line no-restricted-syntax
                unoptimized
                className="object-cover"
              />
            </div>
          ) : (
            <div className="flex h-16 max-w-48 items-center gap-2 rounded-lg border bg-muted px-3 pr-8 text-sm">
              <FileTextIcon className="size-5 shrink-0 text-muted-foreground" />
              <span className="truncate">{one.file.name}</span>
            </div>
          )}
          <Button
            data-component={`${dataComponent}RemoveFile`}
            type="button"
            variant="secondary"
            size="icon-xs"
            aria-label={`Remove ${one.file.name}`}
            className="absolute top-1 right-1"
            disabled={disabled}
            onClick={() => onRemove(one)}
          >
            <XIcon />
          </Button>
        </div>
      ))}
    </div>
  );
}
