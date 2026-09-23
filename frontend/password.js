// Uses the signed-in Supabase client only; no admin key or role changes.
export function passwordView({ client, mode, verifyCurrent, onBack }) {
  const reset = mode === 'forgot';
  const change = mode === 'change';
  const v = document.createElement('div');
  v.className = 'login-wrap';
  const text = (th, en) => `<span data-pw-th="${th}" data-pw-en="${en}">${th}</span>`;
  v.innerHTML = `<form class="login-card password-card">
    <h1>${reset ? text('ลืมรหัสผ่าน', 'Forgot password') : text('ตั้งรหัสผ่านใหม่', 'Set a new password')}</h1>
    <p class="sub">${reset ? text('กรอกอีเมลที่ใช้เข้าสู่ระบบ แล้วเปิดลิงก์ที่ได้รับเพื่อกำหนดรหัสใหม่', 'Enter your sign-in email, then open the emailed link to set a new password.') : text('ใช้วลีที่จำได้ง่ายแต่เดายาก อย่างน้อย 8 ตัวอักษร', 'Use a memorable but hard-to-guess phrase, at least 8 characters.')}</p>
    <div class="password-message" role="status" aria-live="polite"></div>
    ${reset ? `<label for="reset-email">${text('อีเมล', 'Email')}</label><input id="reset-email" type="email" autocomplete="email" required>` : ''}
    ${change ? `<label for="current-password">${text('รหัสผ่านปัจจุบัน', 'Current password')}</label><input id="current-password" type="password" autocomplete="current-password" required>` : ''}
    ${!reset ? `<label for="new-password">${text('รหัสผ่านใหม่', 'New password')}</label><input id="new-password" type="password" autocomplete="new-password" minlength="8" required>
      <label for="confirm-password">${text('ยืนยันรหัสผ่านใหม่', 'Confirm new password')}</label><input id="confirm-password" type="password" autocomplete="new-password" minlength="8" required>
      <label class="password-show"><input type="checkbox" id="show-password"> ${text('แสดงรหัสผ่านใหม่', 'Show new password')}</label>` : ''}
    <button type="submit">${reset ? text('ส่งลิงก์ตั้งรหัสใหม่', 'Send reset link') : text('บันทึกรหัสผ่านใหม่', 'Save new password')}</button>
    <button type="button" class="ghost password-back">${text('กลับ', 'Back')}</button>
  </form>`;
  const localize = () => v.querySelectorAll('[data-pw-th]').forEach(el => {
    el.textContent = window.UI?.language === 'en' ? el.dataset.pwEn : el.dataset.pwTh;
  });
  localize();
  // No document-level listener per form: app calls this when preferences change.
  v.localize = localize;
  v.querySelector('.password-back').onclick = onBack;
  v.querySelector('#show-password')?.addEventListener('change', e => {
    for (const id of ['new-password', 'confirm-password']) v.querySelector('#' + id).type = e.target.checked ? 'text' : 'password';
  });
  const message = (th, en, ok = false) => {
    const box = v.querySelector('.password-message');
    box.className = 'password-message msg ' + (ok ? 'ok' : 'err');
    box.innerHTML = text(th, en); localize();
  };
  v.querySelector('form').onsubmit = async e => {
    e.preventDefault();
    const button = v.querySelector('[type=submit]');
    if (button.disabled) return;
    const password = v.querySelector('#new-password')?.value;
    if (!reset && password !== v.querySelector('#confirm-password').value) {
      message('รหัสผ่านใหม่ทั้งสองช่องไม่ตรงกัน', 'The new passwords do not match.'); return;
    }
    if (!reset && (password.length < 8 || !password.trim())) {
      message('กรุณาตั้งรหัสผ่านอย่างน้อย 8 ตัวอักษร', 'Please use at least 8 characters.'); return;
    }
    button.disabled = true;
    try {
      if (reset) {
        const { error } = await client.auth.resetPasswordForEmail(v.querySelector('#reset-email').value.trim(), {
          redirectTo: location.origin + '/?auth=recovery',
        });
        if (error) throw error;
        // Same response regardless of account existence.
        message('หากมีบัญชีสำหรับอีเมลนี้ คุณจะได้รับลิงก์ตั้งรหัสใหม่ โปรดตรวจกล่องจดหมายและสแปม', 'If this email has an account, you will receive a reset link. Check your inbox and spam folder.', true);
      } else {
        if (change && !await verifyCurrent(v.querySelector('#current-password').value)) {
          message('รหัสผ่านปัจจุบันไม่ถูกต้อง กรุณาลองใหม่', 'The current password is incorrect. Please try again.'); return;
        }
        const { error } = await client.auth.updateUser({ password });
        if (error) throw error;
        v.querySelectorAll('input').forEach(el => { if (el.type !== 'checkbox') el.value = ''; });
        message('เปลี่ยนรหัสผ่านสำเร็จ ครั้งถัดไปให้ใช้รหัสผ่านใหม่เข้าสู่ระบบ', 'Password changed successfully. Use your new password the next time you sign in.', true);
        v.querySelectorAll('input').forEach(el => el.disabled = true);
        button.hidden = true;
      }
    } catch (error) {
      // Do not render provider messages that may contain account details.
      if (error.status === 429 || /rate_limit/.test(error.code || '')) {
        message('ส่งคำขอบ่อยเกินไป กรุณารอสักครู่แล้วลองใหม่', 'Too many requests. Please wait and try again.');
      } else if (['weak_password', 'same_password'].includes(error.code)) {
        message('รหัสใหม่ไม่ผ่านข้อกำหนดหรือซ้ำกับรหัสเดิม กรุณาใช้รหัสอื่น', 'The password is too weak or unchanged. Please choose a different password.');
      } else {
        message('ดำเนินการไม่สำเร็จ ลิงก์อาจหมดอายุหรือบริการอีเมลยังไม่พร้อม กรุณาลองใหม่หรือติดต่อผู้ดูแล', 'Unable to complete the request. The link may have expired or email delivery may be unavailable. Try again or contact your administrator.');
      }
    } finally { button.disabled = false; }
  };
  return v;
}
