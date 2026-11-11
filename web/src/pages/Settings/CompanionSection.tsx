import CharacterSection from "./CharacterSection";
import ProfileSection from "./ProfileSection";
import MasterIdentitySection from "./MasterIdentitySection";
import MemorySection from "./MemorySection";

// 伙伴：角色卡 / 她的自我画像 / 主人身份 / 记忆 四段顺排。
// 原来的二级 Tabs 已删除——设置变成一栏到底的分组滚动页，子页签没有存在价值。
export default function CompanionSection() {
  return (
    <>
      <CharacterSection />
      <ProfileSection />
      <MasterIdentitySection />
      <MemorySection />
    </>
  );
}
