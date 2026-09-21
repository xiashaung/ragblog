<script setup lang="ts">
/**
 * 账号管理 —— 后台账号的增删改 + 角色分配（RBAC 管理面）。
 *
 * 权限：读需 account:read（路由已挡），写需 account:manage；
 * 按钮级：新建 / 分配角色 / 重置密码只由 v-perm 控制，「不能停用 / 删除自己」这类
 * 逐行判定继续留在 :disabled 里（两者叠加生效，互不覆盖）；
 * 弹窗的「确定」按钮同样按权限禁用（okProps），处理函数里再留一道 guard。
 * 后端还会独立校验「不能停用/删除自己」「不能收回最后一个超管」等守卫。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  message,
} from 'ant-design-vue';
import { rbacApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import { useAuthStore } from '@/store/modules/auth';
import type { AccountItem, RoleItem } from '@/typings';

const auth = useAuthStore();
const { can, tip: permHint, need, okProps } = usePerm();
const canManage = computed(() => can('account:manage'));

/**
 * 写操作兜底闸门：确定按钮已经按权限置灰，但回车提交、权限中途被收等路径
 * 仍可能走到这里，所以发请求前再问一次；返回 true 表示「被拦下了」。
 */
function guardManage() {
  if (canManage.value) return false;
  message.warning(need('account:manage'));
  return true;
}

const list = ref<AccountItem[]>([]);
const roles = ref<RoleItem[]>([]);
const loading = ref(false);
const saving = ref(false);

const createOpen = ref(false);
const form = reactive({ username: '', name: '', password: '', roles: ['viewer'] as string[] });

const pwdOpen = ref(false);
const pwdTarget = ref<AccountItem | null>(null);
const pwdValue = ref('');

const roleOpen = ref(false);
const roleTarget = ref<AccountItem | null>(null);
const roleValue = ref<string[]>([]);

const deleteTarget = ref<AccountItem | null>(null);

const roleOptions = computed(() =>
  roles.value.map((item) => ({ label: `${item.name}（${item.code}）`, value: item.code })),
);

const columns = [
  { title: '用户名', dataIndex: 'username', key: 'username' },
  { title: '姓名', dataIndex: 'name', key: 'name' },
  { title: '角色', key: 'roles' },
  { title: '状态', key: 'status', width: 120 },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '操作', key: 'actions', width: 260 },
];

function roleName(code: string) {
  return roles.value.find((item) => item.code === code)?.name || code;
}

function fail(e: unknown, fallback: string) {
  const err = e as { detail?: string; message?: string };
  message.error(err?.detail || err?.message || fallback);
}

async function load() {
  loading.value = true;
  try {
    list.value = await rbacApi.accounts();
  } catch (e) {
    list.value = [];
    fail(e, '账号列表加载失败');
  } finally {
    loading.value = false;
  }
  if (auth.hasPerm('role:read')) {
    try {
      roles.value = await rbacApi.roles();
    } catch {
      roles.value = [];
    }
  }
}

async function onCreate() {
  if (guardManage()) return;
  if (!form.username.trim() || form.password.length < 8) {
    message.warning('用户名必填，密码至少 8 位');
    return;
  }
  saving.value = true;
  try {
    await rbacApi.createAccount({
      username: form.username.trim(),
      password: form.password,
      name: form.name.trim(),
      roles: form.roles,
    });
    message.success('账号已创建');
    createOpen.value = false;
    form.username = '';
    form.name = '';
    form.password = '';
    form.roles = ['viewer'];
    await load();
  } catch (e) {
    fail(e, '创建失败');
  } finally {
    saving.value = false;
  }
}

async function onToggle(row: AccountItem, checked: boolean) {
  if (guardManage()) return;
  try {
    await rbacApi.updateAccount(row.id, { is_active: checked });
    message.success(checked ? '已启用' : '已停用并强制下线');
    await load();
  } catch (e) {
    fail(e, '状态更新失败');
    await load();
  }
}

async function onResetPassword() {
  if (guardManage()) return;
  if (!pwdTarget.value || pwdValue.value.length < 8) {
    message.warning('新密码至少 8 位');
    return;
  }
  try {
    await rbacApi.updateAccount(pwdTarget.value.id, { password: pwdValue.value });
    message.success('密码已重置，该账号需重新登录');
    pwdOpen.value = false;
    pwdValue.value = '';
  } catch (e) {
    fail(e, '重置失败');
  }
}

function openRoles(row: AccountItem) {
  roleTarget.value = row;
  roleValue.value = [...row.roles];
  roleOpen.value = true;
}

async function onAssignRoles() {
  if (guardManage()) return;
  if (!roleTarget.value) return;
  try {
    await rbacApi.setAccountRoles(roleTarget.value.id, roleValue.value);
    message.success('角色已更新');
    roleOpen.value = false;
    await load();
  } catch (e) {
    fail(e, '角色分配失败');
  }
}

async function onRemove() {
  if (guardManage()) return;
  if (!deleteTarget.value) return;
  try {
    await rbacApi.removeAccount(deleteTarget.value.id);
    message.success('账号已删除');
    deleteTarget.value = null;
    await load();
  } catch (e) {
    fail(e, '删除失败');
    deleteTarget.value = null;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-card">
    <Alert
      v-if="!canManage"
      type="info"
      show-icon
      style="margin-bottom: 16px"
      message="只读模式"
      description="当前账号只有查看权限（account:read），需要新建账号或调整角色请找超级管理员。"
    />

    <Card title="后台账号" :bordered="false">
      <template #extra>
        <Space>
          <Button @click="load">刷新</Button>
          <Button v-perm="'account:manage'" type="primary" @click="createOpen = true">新建账号</Button>
        </Space>
      </template>
      <Table
        :data-source="list"
        :columns="columns"
        :loading="loading"
        row-key="id"
        size="middle"
        :pagination="{ pageSize: 10, hideOnSinglePage: true }"
      >
        <!-- ant-design-vue 4 只支持 #bodyCell 这一个单元格插槽，按 column.key 分支渲染 -->
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'roles'">
            <Space wrap>
              <Tag v-for="code in record.roles" :key="code" :color="code === 'superadmin' ? 'gold' : 'blue'">
                {{ roleName(code) }}
              </Tag>
              <span v-if="!record.roles.length" style="color: var(--app-muted)">未分配角色</span>
            </Space>
          </template>
          <template v-else-if="column.key === 'status'">
            <Tooltip :title="canManage ? '' : permHint('account:manage')">
              <Switch
                :checked="record.is_active"
                :disabled="!canManage || record.id === auth.userInfo?.id"
                checked-children="启用"
                un-checked-children="停用"
                @change="(checked: boolean) => onToggle(record, checked)"
              />
            </Tooltip>
          </template>
          <template v-else-if="column.key === 'created_at'">
            {{ record.created_at ? new Date(record.created_at * 1000).toLocaleString() : '-' }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <Space wrap>
              <Button size="small" v-perm="'account:manage'" @click="openRoles(record)">分配角色</Button>
              <Button
                size="small"
                v-perm="'account:manage'"
                @click="
                  () => {
                    pwdTarget = record;
                    pwdOpen = true;
                  }
                "
              >
                重置密码
              </Button>
              <Button
                size="small"
                danger
                v-perm="'account:manage'"
                :disabled="record.id === auth.userInfo?.id"
                @click="deleteTarget = record"
              >
                删除
              </Button>
            </Space>
          </template>
        </template>
      </Table>
    </Card>

    <Modal
      v-model:open="createOpen"
      title="新建账号"
      :confirm-loading="saving"
      :ok-button-props="okProps('account:manage')"
      @ok="onCreate"
    >
      <Form layout="vertical">
        <Form.Item label="用户名" required>
          <Input v-model:value="form.username" placeholder="登录用，全局唯一" />
        </Form.Item>
        <Form.Item label="姓名">
          <Input v-model:value="form.name" placeholder="展示名，默认与用户名相同" />
        </Form.Item>
        <Form.Item label="初始密码" required>
          <Input.Password v-model:value="form.password" placeholder="至少 8 位" />
        </Form.Item>
        <Form.Item label="角色">
          <Select
            v-model:value="form.roles"
            mode="multiple"
            :options="roleOptions"
            placeholder="可多选；分配超级管理员需你本人是超管"
          />
        </Form.Item>
      </Form>
    </Modal>

    <Modal
      v-model:open="pwdOpen"
      :title="`重置密码：${pwdTarget?.username || ''}`"
      :ok-button-props="okProps('account:manage')"
      @ok="onResetPassword"
    >
      <Alert
        type="warning"
        show-icon
        style="margin-bottom: 12px"
        message="重置密码会同时注销该账号的所有在线会话"
      />
      <Input.Password v-model:value="pwdValue" placeholder="新密码，至少 8 位" />
    </Modal>

    <Modal
      v-model:open="roleOpen"
      :title="`分配角色：${roleTarget?.username || ''}`"
      :ok-button-props="okProps('account:manage')"
      @ok="onAssignRoles"
    >
      <Alert
        type="info"
        show-icon
        style="margin-bottom: 12px"
        message="覆盖式设置：保存后该账号只拥有下面勾选的角色"
      />
      <Select v-model:value="roleValue" mode="multiple" :options="roleOptions" style="width: 100%" />
    </Modal>

    <Modal
      :open="!!deleteTarget"
      title="删除账号"
      ok-text="删除"
      :ok-button-props="okProps('account:manage', { danger: true })"
      @cancel="deleteTarget = null"
      @ok="onRemove"
    >
      <p>确定删除账号【{{ deleteTarget?.username }}】？其在线会话会立即失效。</p>
    </Modal>
  </div>
</template>
