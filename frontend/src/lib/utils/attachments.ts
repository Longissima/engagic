// An attachment is a whole document, or a page span of one: chunked packets
// have no per-item file, so an item cites the packet pages its text came from.
interface AttachmentLink {
	name?: string;
	url: string;
	portal_url?: string;
	pages?: [number, number];
}

export function attachmentHref(attachment: AttachmentLink): string {
	if (attachment.pages) return `${attachment.url}#page=${attachment.pages[0]}`;
	return attachment.portal_url || attachment.url;
}

// Several spans share one packet URL, so the URL alone is not a unique key.
export function attachmentKey(attachment: AttachmentLink): string {
	return `${attachment.url}#${attachment.pages?.join('-') ?? ''}#${attachment.name ?? ''}`;
}

export function attachmentPagesLabel(attachment: AttachmentLink): string {
	const pages = attachment.pages;
	if (!pages) return '';
	return pages[0] === pages[1] ? ` (p. ${pages[0]})` : ` (pp. ${pages[0]}-${pages[1]})`;
}
