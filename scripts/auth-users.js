#!/usr/bin/env node
// auth-users.js — manage Love Refactored login accounts (data/auth/users.json).
//
// Usage:
//   node scripts/auth-users.js list
//   node scripts/auth-users.js add <username> --role admin
//   node scripts/auth-users.js add <username> --role guest --companion Aria
//   node scripts/auth-users.js set-password <username>
//   node scripts/auth-users.js remove <username>
//
// Passwords are prompted interactively (hidden) — never passed on the
// command line, so they don't end up in your shell history.

const fs = require('fs');
const path = require('path');

const { hashPassword } = require(
  fs.existsSync(path.join(__dirname, '..', 'auth.js'))
    ? path.join(__dirname, '..', 'auth.js')
    : path.join(__dirname, '..', 'auth.example.js')
);

const AUTH_DIR = path.join(__dirname, '..', 'data', 'auth');
const USERS_FILE = path.join(AUTH_DIR, 'users.json');

function loadUsersFile() {
  try {
    const raw = JSON.parse(fs.readFileSync(USERS_FILE, 'utf-8'));
    if (raw && typeof raw.users === 'object') return raw;
  } catch (_e) {}
  return { users: {} };
}

function saveUsersFile(data) {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const tmp = USERS_FILE + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(tmp, USERS_FILE);
}

function promptHidden(question) {
  return new Promise((resolve, reject) => {
    process.stdout.write(question);
    const stdin = process.stdin;
    if (!stdin.isTTY) {
      // Piped input (e.g. echo "pass" | node …) — read one line.
      let buf = '';
      stdin.setEncoding('utf8');
      stdin.on('data', (d) => { buf += d; });
      stdin.on('end', () => resolve(buf.split('\n')[0]));
      return;
    }
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding('utf8');
    let value = '';
    const onData = (ch) => {
      if (ch === '\r' || ch === '\n') {
        stdin.setRawMode(false);
        stdin.pause();
        stdin.removeListener('data', onData);
        process.stdout.write('\n');
        resolve(value);
      } else if (ch === '\u0003') { // Ctrl-C
        stdin.setRawMode(false);
        process.stdout.write('\n');
        reject(new Error('cancelled'));
      } else if (ch === '\u007f' || ch === '\b') {
        value = value.slice(0, -1);
      } else {
        value += ch;
      }
    };
    stdin.on('data', onData);
  });
}

function validUsername(name) {
  return typeof name === 'string' && /^[a-z0-9_-]{1,32}$/.test(name);
}

function getFlag(args, flag) {
  const i = args.indexOf(flag);
  return i !== -1 && args[i + 1] ? args[i + 1] : null;
}

async function promptNewPassword() {
  const pass = await promptHidden('New password (min 12 chars): ');
  if (pass.length < 12) {
    console.error('✗ Password must be at least 12 characters. (Tip: a few random words beats Symb0l$oup.)');
    process.exit(1);
  }
  const confirm = await promptHidden('Confirm password: ');
  if (pass !== confirm) {
    console.error('✗ Passwords do not match.');
    process.exit(1);
  }
  return pass;
}

async function main() {
  const [cmd, usernameArg, ...rest] = process.argv.slice(2);
  const username = usernameArg ? usernameArg.toLowerCase() : null;
  const data = loadUsersFile();

  switch (cmd) {
    case 'list': {
      const names = Object.keys(data.users);
      if (names.length === 0) {
        console.log('No accounts yet. Create one with: node scripts/auth-users.js add <username> --role admin');
        return;
      }
      for (const name of names) {
        const u = data.users[name];
        console.log(`  ${name}  (${u.role}${u.companion ? `, companion: ${u.companion}` : ''})`);
      }
      return;
    }

    case 'add': {
      if (!validUsername(username)) {
        console.error('✗ Username must be 1-32 chars: lowercase letters, numbers, _ or -');
        process.exit(1);
      }
      if (data.users[username]) {
        console.error(`✗ "${username}" already exists. Use set-password to change their password.`);
        process.exit(1);
      }
      const role = getFlag(rest, '--role');
      if (role !== 'admin' && role !== 'guest') {
        console.error('✗ Specify --role admin or --role guest');
        process.exit(1);
      }
      const companion = getFlag(rest, '--companion');
      if (role === 'guest' && !companion) {
        console.error('✗ Guests need --companion <Name> (the one companion they may talk to)');
        process.exit(1);
      }
      const pass = await promptNewPassword();
      data.users[username] = {
        passwordHash: hashPassword(pass),
        role,
        ...(companion ? { companion } : {}),
        createdAt: new Date().toISOString()
      };
      saveUsersFile(data);
      console.log(`✓ Added ${role} account "${username}"${companion ? ` (companion: ${companion})` : ''}`);
      return;
    }

    case 'set-password': {
      if (!username || !data.users[username]) {
        console.error(`✗ No account named "${username}". Use list to see accounts.`);
        process.exit(1);
      }
      const pass = await promptNewPassword();
      data.users[username].passwordHash = hashPassword(pass);
      data.users[username].passwordChangedAt = new Date().toISOString();
      saveUsersFile(data);
      console.log(`✓ Password updated for "${username}"`);
      return;
    }

    case 'remove': {
      if (!username || !data.users[username]) {
        console.error(`✗ No account named "${username}".`);
        process.exit(1);
      }
      delete data.users[username];
      saveUsersFile(data);
      console.log(`✓ Removed "${username}"`);
      return;
    }

    default:
      console.log(`Manage Love Refactored login accounts.

Usage:
  node scripts/auth-users.js list
  node scripts/auth-users.js add <username> --role admin
  node scripts/auth-users.js add <username> --role guest --companion <Name>
  node scripts/auth-users.js set-password <username>
  node scripts/auth-users.js remove <username>`);
      process.exit(cmd ? 1 : 0);
  }
}

main().catch((err) => {
  console.error(err.message === 'cancelled' ? 'Cancelled.' : `✗ ${err.message}`);
  process.exit(1);
});
