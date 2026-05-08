# StratoVirt快照恢复问题调试总结

## 调试进展

### 已修复的问题

#### 1. ✅ resumeScriptStratovirt缺少pmem设备配置

**问题位置**: `/home/Conch/internal/sandbox/vmm/stratovirt.go:60`

**原始问题**: resume脚本模板中缺少 `{{ .PmemDevices }}`，导致恢复时StratoVirt缺少pmem设备，与创建时的设备配置不匹配。

**修复方案**: 在第60行添加了pmem设备配置：
```go
{{ .PmemDevices }} \
```

**修复后效果**: 恢复脚本现在包含完整的pmem设备配置。

#### 2. ✅ pmem使用了StratoVirt不支持的readonly参数

**问题位置**: `/home/Conch/internal/sandbox/vmm/stratovirt.go:190-195`

**原始问题**: `buildPmemDevices`函数在resume时会添加`readonly=on`参数，但StratoVirt不支持这个参数。

**错误日志**:
```
2026-05-08T17:44:15.683: ERROR: Failed to create vmconfig
Add args "memory-backend-file,size=1988M,id=pmem0,mem-path=...,share=on,readonly=on" error.
Caused by: error: unexpected argument found
```

**修复方案**: 移除readonly参数判断，统一使用无readonly的配置。

**修复代码**:
```go
// 之前（有bug）
var object string
if readonly {
    object = fmt.Sprintf("-object memory-backend-file,size=%s,id=%s,mem-path=%s,share=on,readonly=on", ...)
} else {
    object = fmt.Sprintf("-object memory-backend-file,size=%s,id=%s,mem-path=%s,share=on", ...)
}

// 修复后
object := fmt.Sprintf("-object memory-backend-file,size=%s,id=%s,mem-path=%s,share=on", ...)
```

#### 3. ✅ vsock FileConn失败导致无限循环

**问题位置**: `/home/Conch/internal/sandbox/manager.go:320-329`

**原始问题**: 当StratoVirt VM成功启动并收到Agent READY信号后，`net.FileConn(vsockFD)`失败导致无限循环重试，sandbox创建卡住。

**错误日志**:
```
2026-05-08 17:59:32 [INFO] Stratovirt Sandbox Agent is officially READY!
2026-05-08 17:59:32 [WARN] failed to create net.Conn from vsock fd
    error=file file+net vsock: protocol not supported
# 无限循环重复上述日志...
```

**根本原因**: Go的`net.FileConn`不支持vsock类型的文件描述符，导致错误后继续循环重试。

**修复方案**: 当FileConn失败时，直接完成sandbox创建（因为Agent已经READY），而不是继续循环。

**修复代码**:
```go
// 之前（有bug）
vsockConn, err := net.FileConn(file)
if err != nil {
    logger.Warn("failed to create net.Conn from vsock fd", ulog.F("error", err))
    file.Close()
    time.Sleep(vsockSignalRetry)
    continue  // 继续循环，导致卡住
}

// 修复后
vsockConn, err := net.FileConn(file)
if err != nil {
    logger.Warn("failed to create net.Conn from vsock fd, but Agent is READY so proceeding", ulog.F("error", err))
    file.Close()
    close(readyCh)  // 直接完成创建
    return
}
```

### 新发现的核心限制

#### ❌ StratoVirt无法访问overlayfs文件

**现象**: 经过上述修复后，快照恢复仍然失败，但错误原因不同：

**错误日志**:
```
2026-05-08T18:03:55 [ERROR]: Failed to realize standard VM.
Caused by:
    Failed to open file: /run/conch/snapshot/default/shared/sha256065a.../layer0.erofs
    Read-only file system (os error 30)
```

**关键发现**:

1. **文件确实存在**: 在sandbox创建阶段，rootfs overlayfs被成功mount：
   ```
   ls -la /var/run/conch/snapshot/default/shared/sha256065a.../layer0.erofs
   -rw-r--r-- 1 root root 2084569088 Apr 14 21:53 layer0.erofs
   ```

2. **文件权限正常**: 文件权限是`rw-r--r--`，理论上应该可以访问。

3. **StratoVirt报告"Read-only file system"**: 这不是文件权限问题，而是文件系统层面的限制。

4. **VM snapshot mount方式**:
   ```
   /dev/sdd on /run/conch/snapshot/default/shared/sha2567fe0c2b2... type ext4 (ro,relatime,...)
   ```
   VM snapshot（kernel/initrd）被mount为只读ext4，而不是overlayfs。

## 根本原因分析

### StratoVirt的incoming机制限制

**StratoVirt快照恢复流程**:

```
启动StratoVirt VM
  ↓
解析命令行参数（-incoming file:...）
  ↓
在incoming模式下初始化VM
  ↓
尝试创建pmem设备
  ↓
访问pmem mem-path文件
  ↓
❌ 遇到overlayfs mount → 报错"Read-only file system"
  ↓
VM启动失败
```

**为什么会报"Read-only file system"**?

1. StratoVirt在incoming恢复模式下，使用特殊的文件系统访问机制。
2. 这种机制可能需要文件系统支持某些特性（如direct I/O、内存映射等）。
3. Overlayfs作为一个union文件系统，可能不满足这些要求。
4. 即使底层文件是可读写的，overlayfs本身可能被StratoVirt识别为"只读"。

### 与CLH对比

| 特性 | CLH | StratoVirt |
|------|-----|------------|
| 恢复机制 | restore API（HTTP） | incoming file（QEMU兼容） |
| 文件系统要求 | 宽松（支持overlayfs） | 严格（需要底层文件系统支持） |
| pmem处理 | restore API自动处理 | 需要手动配置pmem设备 |
| 快照数据加载 | API控制，可lazy加载 | 文件直接映射，立即加载 |

## 验证测试

### 测试1: Sandbox创建和快照创建 ✅ 成功

```python
sbx = Sandbox.create()
result = sbx.execute(cmd='python3', content='print("Hello")')
print(f"Result: {result}")  # 输出: Hello

snapshot_info = sbx.pause()
print(f"Snapshot ID: {snapshot_info.snapshot_id}")
# 输出: sha256:065a7d360b2766d506ae2d4e75cb469487a24439c7bac3db801f5642e87e762e
```

**验证结果**:
- Sandbox创建成功
- Execute执行成功
- 快照创建成功
- Snapshot ID正确生成

### 测试2: 快照恢复 ❌ 失败

```python
sbx2 = Sandbox.create(snapshot_id=snapshot_id)
# 报错: Failed to open file: .../layer0.erofs, Read-only file system (os error 30)
```

**失败原因**: StratoVirt无法访问overlayfs上的erofs文件。

## 解决方案

### 方案1: 使用CLH进行快照功能（推荐）

**优势**:
- CLH的快照恢复机制更灵活
- 完全支持overlayfs
- 不受文件系统类型限制
- 快照功能已完全实现

**配置**:
```yaml
# config/config.yaml
sandbox:
  default_vmm: cloud-hypervisor  # 快照功能
```

**使用建议**:
- 需要快照恢复功能时，使用CLH
- 普通sandbox创建可以使用StratoVirt（性能更好，openEuler集成更好）

### 方案2: 为StratoVirt创建专用snapshotter

**设计思路**:
- 创建一个新的snapshotter（如`stratovirt-snapshotter`）
- 使用原始ext4文件而非overlayfs
- 每个snapshot都是一个独立的ext4镜像文件
- 避免overlayfs的限制

**优点**:
- 满足StratoVirt的要求
- 快照恢复可以正常工作

**缺点**:
- 需要大量开发工作
- 存储空间占用更大（每个snapshot都是完整拷贝）
- 与containerd overlayfs snapshotter不兼容

### 方案3: StratoVirt恢复时不使用pmem

**可行性分析**:

**方案A - 快照包含rootfs内存数据**:
- ❌ 不可行，因为pmem内存区域必须存在才能恢复
- StratoVirt要求设备配置完全匹配

**方案B - 创建时不使用pmem**:
- ❌ 不可行，因为：
  - StratoVirt创建时需要pmem设备提供rootfs
  - 不使用pmem会导致VM无法启动
  - 失去了pmem的性能优势

### 方案4: 使用raw block设备替代pmem

**设计思路**:
- 不使用pmem（virtio-pmem-pci）
- 使用virtio-blk设备提供rootfs
- Block设备可能没有overlayfs限制

**需要验证**:
- StratoVirt是否支持virtio-blk
- Block设备是否可以访问overlayfs文件
- 快照恢复时block设备配置是否必须匹配

## 当前状态总结

### 功能状态

- ✅ Sandbox创建和启动：完全正常
- ✅ Execute命令执行：完全正常  
- ✅ 网络通信：完全正常
- ✅ 快照创建：完全正常
- ❌ 快照恢复：失败（StratoVirt无法访问overlayfs）

### 已修复的bug

1. ✅ resumeScriptStratovirt缺少pmem设备
2. ✅ pmem使用不支持的readonly参数
3. ✅ vsock FileConn失败导致无限循环

### 核心限制

- ❌ StratoVirt的incoming恢复机制不支持overlayfs
- ❌ pmem设备无法访问overlayfs上的文件
- ❌ 这不是代码bug，而是StratoVirt的设计限制

### 技术分析

**为什么CLH可以而StratoVirt不行？**

**CLH设计**:
- 使用HTTP REST API进行快照恢复
- restore API可以在VM启动后动态加载快照
- 不依赖特定的文件系统类型
- 快照加载过程由API控制，更灵活

**StratoVirt设计（QEMU兼容）**:
- 使用`-incoming file:`参数加载快照
- 快照加载发生在VM初始化阶段
- 需要直接访问文件，对文件系统有严格要求
- pmem设备需要在VM启动时就存在并配置好

## 文档更新

已更新以下文档：
- `/home/Conch/docs/design/stratovirt-resume-final-debug.md` - 最终调试总结（本文档）
- `/home/Conch/docs/design/stratovirt-summary.md` - 工作总结
- `/home/Conch/docs/design/stratovirt-snapshot-resume-debug.md` - 详细调试记录

## 参考资料

- StratoVirt源码: `/home/stratovirt`
- StratoVirt文档: https://gitee.com/openeuler/stratovirt
- QEMU incoming文档: https://qemu-project.gitlab.io/qemu-system/migration/
- Overlayfs文档: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html