/**
 * Import of mails and agents from exports of other systems (semicolon CSV).
 *
 * Mails:        time;sender;subject;text        (time ISO 8601, UTC)
 * Agents:       id;name;skills (|-separated);online (0/1);max_open
 */

function splitCsvLine(line) {
  const out = [];
  let cur = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else if (c === '"') {
        quoted = false;
      } else {
        cur += c;
      }
    } else if (c === '"') {
      quoted = true;
    } else if (c === ';') {
      out.push(cur);
      cur = '';
    } else {
      cur += c;
    }
  }
  out.push(cur);
  return out;
}

export function parseMails(text) {
  const mails = [];
  const errors = [];
  text.split('\n').forEach((line, i) => {
    if (!line.trim() || line.startsWith('#')) return;
    const f = splitCsvLine(line);
    if (f.length !== 4) {
      errors.push(`line ${i + 1}: expected 4 fields, found ${f.length}`);
      return;
    }
    const at = Date.parse(f[0].endsWith('Z') ? f[0] : `${f[0]}Z`);
    if (Number.isNaN(at)) {
      errors.push(`line ${i + 1}: invalid time ${f[0]}`);
      return;
    }
    if (!/^[^@\s]+@[^@\s]+$/.test(f[1].trim())) {
      errors.push(`line ${i + 1}: invalid address ${f[1]}`);
      return;
    }
    mails.push({ at, from: f[1].trim(), subject: f[2], body: f[3].replace(/\\n/g, '\n') });
  });
  return { mails, errors };
}

export function parseAgents(text) {
  return text
    .split('\n')
    .filter((l) => l.trim() && !l.startsWith('#'))
    .map((l) => {
      const [id, name, skills, online, maxOpen] = splitCsvLine(l);
      return {
        id: id.trim(),
        name: name.trim(),
        skills: skills ? skills.split('|').map((s) => s.trim()).filter(Boolean) : [],
        online: online?.trim() === '1',
        maxOpen: maxOpen ? Number(maxOpen) : undefined,
      };
    });
}

export { splitCsvLine };
