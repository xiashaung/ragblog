<script setup lang="ts">
/**
 * 角色权限 —— RBAC 角色的增删改与权限勾选。
 *
 * 权限码目录由后端 app/rbac.py 定义（这里只读渲染），角色本身可自由增删。
 * 内置角色不可删；superadmin 恒为全部权限；含 role:manage 的角色只有超管能建/改
 * —— 这些守卫都在 services/rbac_service 里，前端只负责把后端的中文报错显示出来。
 * 按钮级：新建 / 编辑挂 v-perm（role:manage），弹窗的「确定」与权限勾选框同样按权限禁用；
 * 删除保留「内置角色」的逐行 :disabled。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Divider,
  Form,
  Input,
  Modal,
  Row,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from 'ant-design-vue';
import { rbacApi } from '@/api';
import { usePerm } from '@/composables/usePerm';
import type { PermissionDef, RoleItem } from '@/typings';

const { can, need, okProps } = usePerm();
const canManage = computed(() => can('role:manage'));

/** 写操作兜底闸门（确定按钮已按权限置灰，这里拦的是回车提交与权限中途被收） */
function guardManage() {
  if (canManage.value) return false;
  message.warning(need('role:manage'));
  return true;
}

const list = ref<RoleItem[]>([]);
const catalog = ref<PermissionDef[]>([]);
const loading = ref(false);
const saving = ref(false);

const editOpen = ref(false);
const editing = ref<RoleItem | null>(null); // null = 新建
const form = reactive({ code: '', name: '', description: '', permissions: [] as string[] });

const deleteTarget = ref<RoleItem | null>(null);

const labelOf = computed(() => {
  const map = new Map<string, string>();
  catalog.value.forEach((item) => map.set(item.code, item.label));
  return map;
});

/** 权限按 group 分块渲染（后端目录里带 group 字段） */
const grouped = computed(() => {
  const groups: { name: string; items: PermissionDef[] }[] = [];
  catalog.value.forEach((item) => {
    const found = groups.find((g) => g.name === item.group);
    if (found) found.items.push(item);
    else groups.push({ name: item.group, items: [item] });
  });
  return groups;
});

// 窄屏（笔记本）兜底：固定列写明宽度 + scroll.x，避开「权限」标签列被挤成多行、
// 整行高度失控；操作列钉右，横向滚动时仍可直接点。
const columns = [
  { title: '角色', key: 'name', width: 190 },
  { title: '标识', dataIndex: 'code', key: 'code', width: 130, ellipsis: true },
  { title: '权限', key: 'permissions' },
  { title: '成员', dataIndex: 'members', key: 'members', width: 70 },
  { title: '类型', key: 'builtin', width: 90, responsive: ['lg'] as const },
  { title: '操作', key: 'actions', width: 160, fixed: 'right' as const },
];

function permissionTexts(row: RoleItem): string[] {
  if (row.permissions.includes('*')) return ['全部权限'];
  return row.permissions.map((code) => labelOf.value.get(code) || code);
}

function fail(e: unknown, fallback: string) {
  const err = e as { detail?: string; message?: string };
  message.error(err?.detail || err?.message || fallback);
}

async function load() {
  loading.value = true;
  try {
    list.value = await rbacApi.roles();
  } catch (e) {
    list.value = [];
    fail(e, '角色列表加载失败');
  } finally {
    loading.value = false;
  }
  try {
    catalog.value = await rbacApi.permissions();
  } catch {
    catalog.value = [];
  }
}

function openCreate() {
  editing.value = null;
  form.code = '';
  form.name = '';
  form.description = '';
  form.permissions = [];
  editOpen.value = true;
}

function openEdit(row: RoleItem) {
  editing.value = row;
  form.code = row.code;
  form.name = row.name;
  form.description = row.description;
  // 通配角色（superadmin）不参与勾选，界面直接置空避免误以为可以取消
  form.permissions = row.permissions.includes('*') ? [] : [...row.permissions];
  editOpen.value = true;
}

async function onSave() {
  if (guardManage()) return;
  if (!form.name.trim()) {
    message.warning('角色名称必填');
    return;
  }
  saving.value = true;
  try {
    if (editing.value) {
      await rbacApi.updateRole(editing.value.id, {
        name: form.name.trim(),
        description: form.description,
        // superadmin 不提交 permissions，避免后端报「恒为全部权限」
        permissions: editing.value.permissions.includes('*') ? undefined : form.permissions,
      });
      message.success('角色已更新');
    } else {
      if (!form.code.trim()) {
        message.warning('新建角色需要标识（英文）');
        return;
      }
      await rbacApi.createRole({
        code: form.code.trim().toLowerCase(),
        name: form.name.trim(),
        description: form.description,
        permissions: form.permissions,
      });
      message.success('角色已创建');
    }
    editOpen.value = false;
    await load();
  } catch (e) {
    fail(e, '保存失败');
  } finally {
    saving.value = false;
  }
}

async function onRemove() {
  if (guardManage()) return;
  if (!deleteTarget.value) return;
  try {
    await rbacApi.removeRole(deleteTarget.value.id);
    message.success('角色已删除');
  } catch (e) {
    fail(e, '删除失败');
  } finally {
    deleteTarget.value = null;
    await load();
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
      description="当前账号缺少 role:manage，可以查看角色但不能新建或改动权限。"
    />

    <Card title="角色与权限" :bordered="false">
      <template #extra>
        <Space>
          <Button @click="load">刷新</Button>
          <Button v-perm="'role:manage'" type="primary" @click="openCreate">新建角色</Button>
        </Space>
      </template>
      <Table
        :data-source="list"
        :columns="columns"
        :loading="loading"
        row-key="id"
        size="middle"
        :scroll="{ x: 1000 }"
        :pagination="false"
      >
        <!-- ant-design-vue 4 只支持 #bodyCell 这一个单元格插槽，按 column.key 分支渲染 -->
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <div>{{ record.name }}</div>
            <div v-if="record.description" style="color: var(--app-muted); font-size: 12px">
              {{ record.description }}
            </div>
          </template>
          <template v-else-if="column.key === 'permissions'">
            <Space wrap>
              <Tag v-for="text in permissionTexts(record)" :key="text" :color="text === '全部权限' ? 'gold' : 'default'">
                {{ text }}
              </Tag>
              <span v-if="!permissionTexts(record).length" style="color: var(--app-muted)">无任何权限</span>
            </Space>
          </template>
          <template v-else-if="column.key === 'builtin'">
            <Tooltip
              v-if="record.is_builtin"
              title="内置角色：不可删除、标识不可改（超级管理员权限固定为全部权限）"
            >
              <Tag color="purple">内置</Tag>
            </Tooltip>
            <Tag v-else>自定义</Tag>
          </template>
          <template v-else-if="column.key === 'actions'">
            <Space>
              <Button size="small" v-perm="'role:manage'" @click="openEdit(record)">编辑</Button>
              <Button
                size="small"
                danger
                v-perm="'role:manage'"
                :disabled="record.is_builtin"
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
      v-model:open="editOpen"
      :title="editing ? `编辑角色：${editing.name}` : '新建角色'"
      :confirm-loading="saving"
      :ok-button-props="okProps('role:manage')"
      width="640px"
      @ok="onSave"
    >
      <Form layout="vertical">
        <Row :gutter="12">
          <Col :span="12">
            <Form.Item label="标识（code）" required>
              <Input
                v-model:value="form.code"
                :disabled="!!editing"
                placeholder="如 kb_editor"
                @update:value="(v: string) => (form.code = v.toLowerCase())"
              />
            </Form.Item>
          </Col>
          <Col :span="12">
            <Form.Item label="名称" required>
              <Input v-model:value="form.name" placeholder="如 知识库编辑" />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item label="说明">
          <Input v-model:value="form.description" placeholder="这个角色是给谁用的" />
        </Form.Item>

        <Alert
          v-if="editing?.permissions.includes('*')"
          type="warning"
          show-icon
          style="margin-bottom: 12px"
          message="超级管理员恒为全部权限，此处只能改名称与说明"
        />
        <template v-else>
          <Divider style="margin: 8px 0">权限</Divider>
          <div v-for="group in grouped" :key="group.name" style="margin-bottom: 12px">
            <div style="font-weight: 600; margin-bottom: 6px">{{ group.name }}</div>
            <Checkbox.Group v-model:value="form.permissions" :disabled="!canManage" style="width: 100%">
              <Row :gutter="[8, 8]">
                <Col v-for="item in group.items" :key="item.code" :span="12">
                  <Checkbox :value="item.code">
                    <Tooltip :title="`${item.code}：${item.description}`">
                      {{ item.label }}
                    </Tooltip>
                  </Checkbox>
                </Col>
              </Row>
            </Checkbox.Group>
          </div>
        </template>
      </Form>
    </Modal>

    <Modal
      :open="!!deleteTarget"
      title="删除角色"
      ok-text="删除"
      :ok-button-props="okProps('role:manage', { danger: true })"
      @cancel="deleteTarget = null"
      @ok="onRemove"
    >
      <p>
        确定删除角色【{{ deleteTarget?.name }}】？有账号在使用时会被后端拒绝，需要先调整它们的角色。
      </p>
    </Modal>
  </div>
</template>
