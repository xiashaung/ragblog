<script setup lang="ts">
/**
 * 明暗模式切换器 —— 下拉三态：跟随系统 / 浅色 / 深色。
 * 放在顶栏与登录页复用；状态与持久化全在 theme store，本组件只管展示与触发。
 */
import { computed } from 'vue';
import { BgColorsOutlined, DownOutlined } from '@ant-design/icons-vue';
import { useThemeStore, type ThemeMode } from '@/store/modules/theme';

const theme = useThemeStore();

const MODES: { value: ThemeMode; label: string; tip: string }[] = [
  { value: 'system', label: '跟随系统', tip: '' },
  { value: 'light', label: '浅色', tip: '' },
  { value: 'dark', label: '深色', tip: '' },
];

const current = computed(() => MODES.find((m) => m.value === theme.mode) ?? MODES[0]);
</script>

<template>
  <a-dropdown>
    <a-tooltip>
      <a-button type="text" size="small" class="theme-btn">
        <BgColorsOutlined />
        {{ current.label }}
        <DownOutlined style="font-size: 10px" />
      </a-button>
    </a-tooltip>
    <template #overlay>
      <a-menu :selected-keys="[theme.mode]" @click="({ key }) => theme.setMode(key as ThemeMode)">
        <a-menu-item v-for="m in MODES" :key="m.value">{{ m.label }}</a-menu-item>
      </a-menu>
    </template>
  </a-dropdown>
</template>

<style scoped>
.theme-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
