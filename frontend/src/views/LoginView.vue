<template>
  <div class="login-page">
    <div class="login-card">
      <img src="/memobot-avatar.png" class="avatar" alt="MemoBot" />
      <h2>欢迎使用 MemoBot</h2>
      <p class="subtitle">你的专属 AI 记忆伴侣</p>

      <el-form @submit.prevent="onLogin" class="form">
        <el-input
          v-model="phone"
          placeholder="请输入手机号"
          maxlength="11"
          size="large"
          clearable
        >
          <template #prefix>
            <el-icon><Iphone /></el-icon>
          </template>
        </el-input>

        <div class="code-row">
          <el-input
            v-model="code"
            placeholder="验证码"
            maxlength="6"
            size="large"
          >
            <template #prefix>
              <el-icon><Message /></el-icon>
            </template>
          </el-input>
          <el-button
            size="large"
            :disabled="sending || countdown > 0 || !isPhoneValid"
            @click="onSendCode"
          >
            {{ countdown > 0 ? `${countdown}s` : '获取验证码' }}
          </el-button>
        </div>

        <el-button
          type="primary"
          size="large"
          class="submit-btn"
          :loading="loading"
          :disabled="!isPhoneValid || code.length !== 6"
          @click="onLogin"
        >
          登 录 / 注 册
        </el-button>

        <p class="hint">首次登录将自动创建账号</p>
      </el-form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Iphone, Message } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const phone = ref('')
const code = ref('')
const sending = ref(false)
const loading = ref(false)
const countdown = ref(0)
let timer: number | null = null

const isPhoneValid = computed(() => /^1[3-9]\d{9}$/.test(phone.value))

function startCountdown() {
  countdown.value = 60
  timer = window.setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  }, 1000)
}

async function onSendCode() {
  if (!isPhoneValid.value) {
    ElMessage.warning('请输入正确的手机号')
    return
  }
  sending.value = true
  try {
    const data = await auth.sendCode(phone.value)
    ElMessage.success(
      data.dev_code
        ? `验证码已发送（开发模式）：${data.dev_code}`
        : '验证码已发送',
    )
    startCountdown()
  } catch (e: any) {
    ElMessage.error(e.message || '发送失败')
  } finally {
    sending.value = false
  }
}

async function onLogin() {
  if (!isPhoneValid.value || code.value.length !== 6) return
  loading.value = true
  try {
    await auth.login(phone.value, code.value)
    ElMessage.success('登录成功')
    router.replace('/chat')
  } catch (e: any) {
    ElMessage.error(e.message || '登录失败')
  } finally {
    loading.value = false
  }
}

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1a1a2e 0%, #2d2d44 100%);
}
.login-card {
  width: 400px;
  padding: 40px 36px;
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.3);
  text-align: center;
}
.avatar {
  width: 88px;
  height: 88px;
  border-radius: 50%;
  object-fit: cover;
  box-shadow: 0 4px 16px rgba(167, 139, 250, 0.4);
}
h2 {
  margin: 16px 0 4px;
  color: #1a1a2e;
}
.subtitle {
  color: #888;
  font-size: 13px;
  margin: 0 0 28px;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.code-row {
  display: flex;
  gap: 10px;
}
.code-row .el-input {
  flex: 1;
}
.submit-btn {
  margin-top: 6px;
  background: #a78bfa;
  border-color: #a78bfa;
}
.submit-btn:hover {
  background: #8b6ff0;
  border-color: #8b6ff0;
}
.hint {
  color: #999;
  font-size: 12px;
  margin: 4px 0 0;
}
</style>
