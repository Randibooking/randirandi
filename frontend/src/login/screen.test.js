// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NORWAY } from './phone.js';
import { setLabels } from '../book/labels.js';
import { SCREEN_AFTER, numberWords, openOn, switchLabel } from './screen.js';

test('a plain value opens on Email and keeps what was typed', () => {
  assert.deepEqual(openOn('randi@example.com'), { screen: 'email', email: 'randi@example.com', country: NORWAY, number: '' });
  assert.equal(openOn('').screen, 'email');
});

test('a remembered value that starts with + opens on the number', () => {
  const opened = openOn('+4791234567');
  assert.equal(opened.screen, 'phone');
  assert.equal(opened.country.code, '+47');
  assert.equal(opened.number, '91234567');
  assert.equal(opened.email, '');
  assert.equal(openOn('+447700900123').country.code, '+44');
});

test('the link switches to the number and "Sign in with email" back', () => {
  assert.equal(SCREEN_AFTER.email, 'phone');
  assert.equal(SCREEN_AFTER[SCREEN_AFTER.email], 'email');
});

test('the links read "What\'s my login?" and "Sign in with email"', () => {
  assert.equal(switchLabel('email'), "What's my login?");
  assert.equal(switchLabel('phone'), 'Sign in with email');
});

test('the links and the words under the number are the page\'s texts, in the organization\'s language', () => {
  setLabels({
    'login.what-login': 'Hva er min innlogging?',
    'login.with-email': 'Logg inn med e-post',
    'login.help-number': 'Skriv mobilnummeret. Kontakt oss: {address}!',
  });
  try {
    assert.equal(switchLabel('email'), 'Hva er min innlogging?');
    assert.equal(switchLabel('phone'), 'Logg inn med e-post');
    const words = numberWords('post@example.org');
    assert.deepEqual([words.before, words.address, words.after], ['Skriv mobilnummeret. Kontakt oss: ', 'post@example.org', '!']);
  } finally {
    setLabels({});
  }
});

test('the number screen names the contact address as a mailto link', () => {
  const words = numberWords('post@example.org');
  assert.equal(words.href, 'mailto:post@example.org');
  assert.equal(words.address, 'post@example.org');
  assert.equal(words.before + words.address + words.after,
    'Enter your mobile number. When you are registered, we send your code by email. If not, contact us: post@example.org.');
});
