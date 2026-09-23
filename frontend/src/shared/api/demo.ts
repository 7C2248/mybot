import { z } from 'zod';
import { characterSchema, type ChatService, type Memory, type Thread } from '../types';

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
export const demoMemories: Memory[] = [
  { id: '12', characterId: 'SuLi', memory: '傍晚的雨停后，我们决定去街角的旧书店。\n她提到书店里有一只橘猫，喜欢趴在窗边的纸箱上。', importance: 6, event_date: '2026-09-12', update_time: '2026-09-12T18:42:00+08:00', keywords: ['书店', '橘猫'] },
  { id: '18', characterId: 'SuLi', memory: '聊天时，我说过比起热闹的地方，更喜欢安静地散步。她记下了这个偏好，建议下次沿河边走。', importance: 5, event_date: '2026-09-12', update_time: '2026-09-12T19:10:00+08:00', keywords: ['散步', '偏好'] },
  { id: '23', characterId: 'SuLi', memory: '我们约好下次继续看那本没有读完的书。\n\n书名还没有记录下来。', importance: null, event_date: null, update_time: null, keywords: [] },
];
export function createDemoThreads(): Thread[] {
  const createdAt = new Date().toISOString();
  return [{
    id: 'demo-suli', characterId: 'SuLi', title: '雨后的傍晚', createdAt,
    draft: '', unread: false, phase: 'idle', messages: [
      { role: 'user', text: '窗外好像安静下来了。\n雨停了吗？' },
      { role: 'assistant', text: '（看了看窗外，放下手里的书）\n嗯，已经停了。路面还是湿的，空气倒是清爽了很多。\n\n要不要出去走走？' },
      { role: 'user', text: '好啊。\n\n去上次那家书店吧。' },
      { role: 'assistant', text: '（拿起搭在椅背上的外套，回过头看你）\n就是街角有只橘猫的那家，对吧？\n走吧，这次应该能赶上它关门之前。' },
    ].map((message, i) => ({ ...message, role: message.role as 'user' | 'assistant', id: `demo-${i}`, createdAt })),
  }];
}

export const demoService: ChatService = {
  mode: 'demo',
  async characters() {
    const response = await fetch('/local-characters/manifest.json');
    if (!response.ok) throw new Error('角色资源未准备好，请运行 npm run assets 后刷新。');
    return z.array(characterSchema).parse(await response.json());
  },
  async memories(characterId) { return demoMemories.filter((item) => item.characterId === characterId); },
  async run(thread, _requestId, emit) {
    emit({ type: 'phase', phase: 'replying' });
    await wait(650);
    emit({ type: 'phase', phase: 'reviewing' });
    await wait(650);
    const round = thread.messages.filter((message) => message.role === 'user').length;
    const replies = [
      '（轻轻点了点头）\n好呀，我们接着聊。\n\n等你准备好了，我们就出发。',
      '（把书签夹进书页，抬眼望向你）\n我在听。\n\n不用着急，慢慢说就好。',
      '嗯，我记住了。\n\n（往你身边靠近了一点）\n接下来，你想去哪里？',
    ];
    emit({ type: 'message.committed', message: { id: crypto.randomUUID(), role: 'assistant', text: replies[round % replies.length], createdAt: new Date().toISOString() } });
  },
};
