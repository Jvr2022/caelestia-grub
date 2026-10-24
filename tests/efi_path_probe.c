/* SPDX-License-Identifier: GPL-3.0-or-later
 * UEFI regression probe: require the source metadata needed by boot managers
 * that open sibling files on their ESP. This is not a Windows implementation.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef u64 status_t;
typedef void *handle_t;
struct guid
{
  u32 a;
  u16 b, c;
  u8 d[8];
};
struct path
{
  u8 type, subtype;
  u16 length;
};
struct loaded_image
{
  u32 revision;
  handle_t parent;
  void *system_table;
  handle_t device;
  struct path *path;
  void *reserved;
  u32 options_size;
  void *options;
  void *base;
  u64 size;
  u32 code_type, data_type;
  void *unload;
};
struct file
{
  u64 revision;
  status_t (*open) (struct file *, struct file **, u16 *, u64, u64);
  status_t (*close) (struct file *);
  void *remove;
  status_t (*read) (struct file *, u64 *, void *);
};
struct filesystem
{
  u64 revision;
  status_t (*open_volume) (struct filesystem *, struct file **);
};
struct boot_services
{
  u8 header[24];
  void *before_handle_protocol[16];
  status_t (*handle_protocol) (handle_t, struct guid *, void **);
};
struct system_table
{
  u8 header[24];
  u16 *vendor;
  u32 revision;
  handle_t input_handle;
  void *input;
  handle_t output_handle;
  void *output;
  handle_t error_handle;
  void *error;
  void *runtime_services;
  struct boot_services *boot_services;
};
_Static_assert (sizeof (void *) == 8, "This probe requires x86_64 UEFI");
_Static_assert (__builtin_offsetof (struct loaded_image, path) == 32,
                "UEFI Loaded Image ABI");
_Static_assert (__builtin_offsetof (struct boot_services, handle_protocol) == 152,
                "UEFI Boot Services ABI");
_Static_assert (__builtin_offsetof (struct system_table, boot_services) == 96,
                "UEFI System Table ABI");
static struct guid loaded_guid = {
  0x5b1b31a1, 0x9562, 0x11d2, { 0x8e, 0x3f, 0, 0xa0, 0xc9, 0x69, 0x72, 0x3b }
};
static struct guid path_guid
    = { 0xbc62157e,
        0x3e33,
        0x4fec,
        { 0x99, 0x20, 0x2d, 0x3b, 0x36, 0xd7, 0x50, 0xdf } };
static struct guid fs_guid = {
  0x964e5b22, 0x6459, 0x11d2, { 0x8e, 0x39, 0, 0xa0, 0xc9, 0x69, 0x72, 0x3b }
};
static u8
inb (u16 port)
{
  u8 value;
  __asm__ volatile ("inb %1,%0" : "=a"(value) : "Nd"(port));
  return value;
}
static void
outb (u16 port, u8 value)
{
  __asm__ volatile ("outb %0,%1" ::"a"(value), "Nd"(port));
}
static void
serial (const char *text)
{
  for (; *text; text++)
    {
      while (!(inb (0x3fd) & 0x20))
        {
        }
      outb (0x3f8, (u8)*text);
    }
}
static status_t
fail (const char *reason)
{
  serial ("CAELESTIA_EFI_PATH_FAILURE:");
  serial (reason);
  serial ("\r\n");
  return 0x8000000000000001ULL;
}
/* Join file path nodes; hardware nodes are skipped for the full image path. */
static int
filename (struct path *node, u16 *name)
{
  unsigned count = 0, limit = 0;
  if (!node)
    return 0;
  while (node->type != 0x7f && limit++ < 64)
    {
      unsigned bytes = node->length;
      if (bytes < 4)
        return 0;
      if (node->type == 4 && node->subtype == 4)
        {
          unsigned i;
          u16 *part = (u16 *)(node + 1);
          if (count && name[count - 1] != '\\' && part[0] != '\\')
            name[count++] = '\\';
          for (i = 0; i < (bytes - 4) / 2 && part[i]; i++)
            {
              if (count >= 250)
                return 0;
              name[count++] = part[i];
            }
        }
      node = (struct path *)((u8 *)node + bytes);
    }
  name[count] = 0;
  return count != 0;
}
static int
equals (u16 *wide, const char *ascii)
{
  unsigned i;
  for (i = 0; ascii[i]; i++)
    if (wide[i] != (u8)ascii[i])
      return 0;
  return !wide[i];
}
status_t
efi_main (handle_t image, struct system_table *system)
{
  struct loaded_image *loaded = 0;
  struct path *full = 0;
  struct filesystem *fs = 0;
  struct file *root = 0, *bcd = 0;
  struct boot_services *bs = system->boot_services;
  u16 path[256], full_path[256];
  char data[64];
  const char *bcd_path = "\\EFI\\Microsoft\\Boot\\BCD";
  const char *marker = "CAELESTIA_EFI_PATH_OK";
  u64 size = sizeof (data);
  unsigned i;
  outb (0x3fb, 0x80);
  outb (0x3f8, 1);
  outb (0x3f9, 0);
  outb (0x3fb, 3);
  if (bs->handle_protocol (image, &loaded_guid, (void **)&loaded) || !loaded)
    return fail ("loaded-image");
  if (!filename (loaded->path, path))
    return fail ("missing-file-path");
  if (!equals (path, "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"))
    return fail ("wrong-file-path");
  if (bs->handle_protocol (image, &path_guid, (void **)&full)
      || !filename (full, full_path))
    return fail ("missing-image-device-path");
  if (!equals (full_path, "\\EFI\\Microsoft\\Boot\\bootmgfw.efi"))
    return fail ("wrong-image-device-path");
  if (!loaded->device
      || bs->handle_protocol (loaded->device, &fs_guid, (void **)&fs) || !fs)
    return fail ("missing-source-filesystem");
  if (fs->open_volume (fs, &root))
    return fail ("open-source-filesystem");
  for (i = 0; bcd_path[i]; i++)
    path[i] = (u8)bcd_path[i];
  path[i] = 0;
  if (root->open (root, &bcd, path, 1, 0))
    return fail ("open-sibling-file");
  if (bcd->read (bcd, &size, data))
    return fail ("read-sibling-file");
  for (i = 0; marker[i]; i++)
    if (i >= size || data[i] != marker[i])
      return fail ("wrong-source-filesystem");
  bcd->close (bcd);
  root->close (root);
  serial ("CAELESTIA_EFI_PATH_OK\r\n");
  for (;;)
    __asm__ volatile ("hlt");
}
